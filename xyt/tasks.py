import functools
import json
import multiprocessing
import os
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yt_dlp

from storage import get_job, upsert_job

DOWNLOADS_DIR = "/app/data/downloads"

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


@_catch_unhandled
def process_video(job_id: str, url: str, transcribe: bool = True):
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS", "DOWNLOADING", "TRANSCRIBING"):
        return

    base_path = os.path.join(DOWNLOADS_DIR, job_id)

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
        "outtmpl": base_path + ".%(ext)s",
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
    job["files"]["mp4"] = f"{job_id}.mp4"

    if not transcribe:
        job["status"] = "SUCCESS"
        job["updated_at"] = _now()
        upsert_job(job)
        return

    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_transcription(job_id, base_path + ".mp4")


def _run_transcription(job_id: str, audio_path: str):
    result_file = tempfile.mktemp(suffix=".json")
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_transcribe_worker, args=(audio_path, result_file))
    proc.start()
    transcribe_started = _now()

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

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    srt_path = os.path.join(DOWNLOADS_DIR, job_id + ".srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(payload["segments"], 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_time(seg['start'])} --> {_fmt_time(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")

    job["status"] = "SUCCESS"
    job["files"]["srt"] = f"{job_id}.srt"
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
