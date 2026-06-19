import functools
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import yt_dlp

from annotate import annotate_executor, annotate_job
from gpu_lock import gpu_lock
from srt_source import stamp_source, stamp_whisper_failed
from storage import get_job, upsert_job

DOWNLOADS_DIR = Path("/app/data/downloads")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")

DOWNLOAD_TIMEOUT = 60 * 60        # 1 hour
TRANSCRIBE_TIMEOUT = 4 * 60 * 60  # 4 hours

# Whisper serialization happens upstream (gpu_lock + whisper container's own
# internal queue), but we keep our own single-threaded executor so this
# process's per-job state (jobs.json updates, file renames) stays serial.
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
    """POST media to the shared whisper service; write returned SRT to srt_path.

    Holds the cross-container GPU lock around the HTTP call so this consumer
    doesn't race marker-pipeline for VRAM. faster-whisper-server has its own
    internal queue for whisper-only contention.

    Pre-flight DELETED check; raises on HTTP / server error.
    """
    current = get_job(job_id)
    if not current or current["status"] == "DELETED":
        return

    with gpu_lock("transcribe-app", f"whisper:{job_id}"):
        # Re-check after acquiring the lock (could have been deleted while we waited).
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            return

        try:
            with open(media_path, "rb") as f:
                resp = requests.post(
                    f"{WHISPER_URL}/v1/audio/transcriptions",
                    files={"file": (media_path.name, f, "application/octet-stream")},
                    data={
                        "model": "whisper-1",  # ignored by fedirz; uses WHISPER__MODEL
                        "response_format": "srt",
                        "temperature": "0",
                        # silero VAD strips silence/music sections before whisper
                        # processes — kills the hallucination loops ("CastingWords",
                        # "Thank you" etc.) that whisper falls into on long files
                        # with extended non-speech audio (movies, podcasts with
                        # instrumental segments). condition_on_previous_text isn't
                        # exposed by fedirz, so VAD is the only knob we have.
                        "vad_filter": "true",
                    },
                    timeout=(10, TRANSCRIBE_TIMEOUT),
                )
        except requests.RequestException as e:
            raise RuntimeError(f"Whisper service call failed: {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(f"Whisper service returned {resp.status_code}: {resp.text[:300]}")

    srt_text = resp.text
    if not srt_text.strip():
        raise RuntimeError("Whisper service returned empty SRT")

    srt_path.write_text(srt_text, encoding="utf-8")
    stamp_source(srt_path, "whisper")


def _queue_annotation(job_id: str):
    """Flip a SUCCESS job to ANNOTATING and submit to the annotate executor.

    Called automatically after every successful transcription (yt + qb).
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
    """yt path: whisper a staging mp4, rename to title-based filename,
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

    # Promote staging mp4 + srt to title-based filenames so they're
    # human-recognizable in Infuse / file listings.
    base = _unique_basename(_sanitize_title(job["title"]))
    final_mp4 = DOWNLOADS_DIR / f"{base}.mp4"
    final_srt = DOWNLOADS_DIR / f"{base}.srt"

    # Rename SRT before mp4 so the sidecar exists before any consumer scans
    # for the freshly-named video — cheap insurance even though WebDAV +
    # Infuse browse on demand (no inotify race).
    staging_path = Path(staging_mp4)
    staging_srt = staging_path.with_suffix(".srt")
    if staging_srt.exists():
        staging_srt.rename(final_srt)
    if staging_path.exists():
        staging_path.rename(final_mp4)

    job["status"] = "SUCCESS"
    job["basename"] = base
    job["updated_at"] = _now()
    upsert_job(job)

    _queue_annotation(job_id)


@_catch_unhandled
def process_qb_file(job_id: str):
    """qb path: whisper an existing file (e.g. /qb/show/ep01.mkv) in place
    and write a sibling SRT. No download, no rename. Auto-queues annotation."""
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS"):
        return

    source_path = job.get("source_path")
    if not source_path:
        _fail(job_id, "qb job missing source_path")
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
        # Stamp the failure onto disk so the background scan loop stops
        # retrying whisper for this file. User clears via the UI ↻ (which
        # deletes the SRT entirely).
        try:
            stamp_whisper_failed(srt_path, str(exc))
        except OSError:
            pass
        _fail(job_id, str(exc))
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    job["status"] = "SUCCESS"
    job["updated_at"] = _now()
    upsert_job(job)

    _queue_annotation(job_id)


def _fail(job_id: str, error: str):
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "FAILED"
    job["error"] = error
    job["updated_at"] = _now()
    upsert_job(job)
