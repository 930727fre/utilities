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

from annotate import annotate_executor, annotate_job
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


def _run_whisper(job_id: str, media_path: Path, srt_path: Path):
    """Run whisper on media_path, write SRT to srt_path. Raises on failure.

    Holds the cross-container GPU lock for the whole transcription.
    Cancellable: if the job flips to DELETED mid-run, terminates the worker
    process and returns silently (caller treats as no-op).
    """
    result_file = tempfile.mktemp(suffix=".json")
    ctx = multiprocessing.get_context("spawn")
    transcribe_started = _now()

    with gpu_lock("transcribe-app", f"whisper:{job_id}"):
        proc = ctx.Process(target=_transcribe_worker, args=(str(media_path), result_file))
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
                raise RuntimeError("Transcription timed out (4 hour limit)")

            current = get_job(job_id)
            if not current or current["status"] == "DELETED":
                proc.terminate()
                proc.join()
                if os.path.exists(result_file):
                    os.unlink(result_file)
                return  # cancelled — caller must check job state before continuing

    if not os.path.exists(result_file):
        raise RuntimeError("Transcription process exited unexpectedly")

    with open(result_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    os.unlink(result_file)

    if "error" in payload:
        raise RuntimeError(payload["error"])

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(payload["segments"], 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_time(seg['start'])} --> {_fmt_time(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")


def _queue_annotation(job_id: str):
    """Flip a SUCCESS job to ANNOTATING and submit to the annotate executor.

    Called automatically after every successful transcription (YouTube + Library).
    """
    job = get_job(job_id)
    if not job or job["status"] != "SUCCESS":
        return
    job["status"] = "ANNOTATING"
    job["annotation_error"] = None
    job["updated_at"] = _now()
    upsert_job(job)
    annotate_executor.submit(annotate_job, job_id)


def _run_transcription(job_id: str, staging_mp4: str):
    """YouTube path: whisper a staging mp4, rename to title-based filename,
    write SRT next to it, then auto-queue annotation."""
    try:
        # Whisper into a temp SRT next to the staging mp4; rename both after.
        staging_path = Path(staging_mp4)
        staging_srt = staging_path.with_suffix(".srt")
        _run_whisper(job_id, staging_path, staging_srt)
    except Exception as exc:
        _fail(job_id, str(exc))
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    # Promote staging mp4 + srt to title-based filenames Jellyfin can use.
    base = _unique_basename(_sanitize_title(job["title"]))
    final_mp4 = DOWNLOADS_DIR / f"{base}.mp4"
    final_srt = DOWNLOADS_DIR / f"{base}.srt"

    staging_path = Path(staging_mp4)
    if staging_path.exists():
        staging_path.rename(final_mp4)
    staging_srt = staging_path.with_suffix(".srt")
    if staging_srt.exists():
        staging_srt.rename(final_srt)

    job["status"] = "SUCCESS"
    job["basename"] = base
    job["updated_at"] = _now()
    upsert_job(job)

    _queue_annotation(job_id)


@_catch_unhandled
def process_library_file(job_id: str):
    """Library path: whisper an existing file (e.g. /qb/show/ep01.mkv) in place
    and write a sibling SRT. No download, no rename. Auto-queues annotation."""
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS"):
        return

    source_path = job.get("source_path")
    if not source_path:
        _fail(job_id, "Library job missing source_path")
        return

    media_path = Path(source_path)
    if not media_path.exists():
        _fail(job_id, f"Source file missing: {source_path}")
        return

    srt_path = media_path.with_suffix(".srt")

    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    try:
        _run_whisper(job_id, media_path, srt_path)
    except Exception as exc:
        _fail(job_id, str(exc))
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    job["status"] = "SUCCESS"
    job["updated_at"] = _now()
    upsert_job(job)

    _queue_annotation(job_id)


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
