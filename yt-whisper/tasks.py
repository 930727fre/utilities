import json
import multiprocessing
import os
import tempfile
import uuid
from datetime import datetime, timezone

from celery import Celery
import yt_dlp

from storage import get_job, upsert_job, read_jobs, write_jobs, ensure_jobs_file

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DOWNLOADS_DIR = "/app/data/downloads"

app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)
app.conf.task_track_started = True
app.conf.broker_connection_retry_on_startup = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.on_after_configure.connect
def _reset_stale_jobs(sender, **kwargs):
    ensure_jobs_file()
    jobs = read_jobs()
    changed = False
    for job in jobs:
        if job["status"] in ("DOWNLOADING", "TRANSCRIBING"):
            job["status"] = "FAILED"
            job["error"] = "Interrupted (worker stopped)"
            job["updated_at"] = _now()
            changed = True
    if changed:
        write_jobs(jobs)


@app.task(bind=True)
def process_video(self, job_id: str, url: str):
    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    base_path = os.path.join(DOWNLOADS_DIR, job_id)

    # --- Download ---
    job["status"] = "DOWNLOADING"
    job["updated_at"] = _now()
    upsert_job(job)

    def progress_hook(d):
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            raise Exception("Job cancelled")

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
    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    # --- Transcribe ---
    result_file = tempfile.mktemp(suffix=".json")
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_transcribe_worker, args=(base_path + ".mp4", result_file))
    proc.start()

    while True:
        proc.join(timeout=2)
        if not proc.is_alive():
            break
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            proc.terminate()
            proc.join()
            os.unlink(result_file) if os.path.exists(result_file) else None
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
    result = payload

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    srt_path = base_path + ".srt"
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(result["segments"], 1):
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
