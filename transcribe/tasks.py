import functools
import json
import multiprocessing
import os
import re
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

from gpu_lock import gpu_lock
from storage import get_job, upsert_job

DOWNLOADS_DIR = Path("/app/data/downloads")

DOWNLOAD_TIMEOUT = 60 * 60        # 1 hour
TRANSCRIBE_TIMEOUT = 4 * 60 * 60  # 4 hours

# Single GPU → serialize work to one job at a time.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe-worker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed(since_iso: str) -> float:
    start = datetime.fromisoformat(since_iso)
    return (datetime.now(timezone.utc) - start).total_seconds()


def _catch_unhandled(fn):
    @functools.wraps(fn)
    def wrapped(job_id, *args, **kwargs):
        try:
            return fn(job_id, *args, **kwargs)
        except Exception as exc:
            traceback.print_exc()
            _fail(job_id, f"Unhandled error: {exc}")
    return wrapped


def enumerate_playlist(url: str) -> list[dict]:
    """Return [{id, title, url}, ...] for a YouTube playlist URL.

    extract_flat=True walks the playlist index without downloading any video.
    Unavailable / deleted entries surface as None and are dropped.
    """
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": e.get("title") or "",
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    return out


@_catch_unhandled
def process_video(job_id: str, url: str):
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS", "DOWNLOADING", "TRANSCRIBING"):
        return

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    staging_base = DOWNLOADS_DIR / job_id   # UUID-named while download/transcribe in progress

    job["status"] = "DOWNLOADING"
    job["updated_at"] = _now()
    upsert_job(job)

    download_started = _now()

    def progress_hook(d):
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            raise Exception("Job cancelled")
        if _elapsed(download_started) > DOWNLOAD_TIMEOUT:
            raise Exception("Download timed out (1 hour limit)")

    ydl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]",
        "merge_output_format": "mp4",
        "outtmpl": str(staging_base) + ".%(ext)s",
        "progress_hooks": [progress_hook],
        "quiet": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", job_id)
    except Exception as e:
        _fail(job_id, str(e))
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    job["title"] = title
    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_transcription(job_id, str(staging_base) + ".mp4")


def _sanitize_title(title: str) -> str:
    """Strip filesystem-unsafe chars; cap length; fallback if empty."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
    safe = safe.strip('. \t\n')
    return safe[:180] or "untitled"


def _unique_basename(base: str) -> str:
    """Suffix `(2)`, `(3)`... if a name with this base already has files."""
    candidate = base
    i = 2
    while (DOWNLOADS_DIR / f"{candidate}.mp4").exists() or (DOWNLOADS_DIR / f"{candidate}.srt").exists():
        candidate = f"{base} ({i})"
        i += 1
    return candidate


def _run_transcription(job_id: str, staging_mp4: str):
    result_file = tempfile.mktemp(suffix=".json")
    ctx = multiprocessing.get_context("spawn")
    transcribe_started = _now()

    with gpu_lock("transcribe-app", f"whisper:{job_id}"):
        proc = ctx.Process(target=_transcribe_worker, args=(staging_mp4, result_file))
        proc.start()

        while True:
            proc.join(timeout=2)
            if not proc.is_alive():
                break

            if _elapsed(transcribe_started) > TRANSCRIBE_TIMEOUT:
                proc.terminate()
                proc.join()
                if os.path.exists(result_file):
                    os.unlink(result_file)
                _fail(job_id, "Transcription timed out (4 hour limit)")
                return

            current = get_job(job_id)
            if not current or current["status"] == "DELETED":
                proc.terminate()
                proc.join()
                if os.path.exists(result_file):
                    os.unlink(result_file)
                return

    if not os.path.exists(result_file):
        _fail(job_id, "Transcription process exited unexpectedly")
        return

    with open(result_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    os.unlink(result_file)

    if "error" in payload:
        _fail(job_id, payload["error"])
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    # Promote the staging mp4 to a title-based filename Jellyfin can use.
    base = _unique_basename(_sanitize_title(job["title"]))
    final_mp4 = DOWNLOADS_DIR / f"{base}.mp4"
    final_srt = DOWNLOADS_DIR / f"{base}.srt"

    staging_path = Path(staging_mp4)
    if staging_path.exists():
        staging_path.rename(final_mp4)

    with open(final_srt, "w", encoding="utf-8") as f:
        for i, seg in enumerate(payload["segments"], 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_time(seg['start'])} --> {_fmt_time(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")

    job["status"] = "SUCCESS"
    job["basename"] = base
    job["updated_at"] = _now()
    upsert_job(job)


def _transcribe_worker(mp4_path: str, result_file: str):
    import json
    import torch
    import whisper
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[transcribe] device={device}", flush=True)
    try:
        model = whisper.load_model("medium", device=device)
        result = model.transcribe(mp4_path, beam_size=5, language=None, verbose=False)
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f)


def _fail(job_id: str, error: str):
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "FAILED"
    job["error"] = error
    job["updated_at"] = _now()
    upsert_job(job)


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
