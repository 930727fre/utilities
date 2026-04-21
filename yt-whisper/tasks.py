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
SCAN_DIR = os.getenv("SCAN_DIR", "/app/data/inbox")

DOWNLOAD_TIMEOUT = 60 * 60        # 1 hour
TRANSCRIBE_TIMEOUT = 4 * 60 * 60  # 4 hours

app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)
app.conf.task_track_started = True
app.conf.broker_connection_retry_on_startup = True
app.conf.beat_schedule = {
    "scan-inbox": {
        "task": "tasks.scan_inbox_task",
        "schedule": 5.0,  # every 5 seconds
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed(since_iso: str) -> float:
    start = datetime.fromisoformat(since_iso)
    return (datetime.now(timezone.utc) - start).total_seconds()


@app.on_after_configure.connect
def _recover_jobs(sender, **kwargs):
    ensure_jobs_file()
    jobs = read_jobs()
    changed = False
    for job in jobs:
        if job["status"] in ("DOWNLOADING", "TRANSCRIBING", "PENDING"):
            job["status"] = "PENDING"
            job["error"] = None
            job["updated_at"] = _now()
            changed = True
    if changed:
        write_jobs(jobs)

    for job in read_jobs():
        if job["status"] == "PENDING":
            if job.get("source_file"):
                src_path = os.path.join(SCAN_DIR, job["source_file"])
                transcribe_file.apply_async(args=[job["job_id"], src_path], task_id=job["job_id"])
            else:
                process_video.apply_async(args=[job["job_id"], job["url"]], task_id=job["job_id"])


@app.task(bind=True)
def process_video(self, job_id: str, url: str):
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS", "DOWNLOADING", "TRANSCRIBING"):
        return

    base_path = os.path.join(DOWNLOADS_DIR, job_id)

    # --- Download ---
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
    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_transcription(job_id, base_path + ".mp4")


@app.task(bind=True)
def transcribe_file(self, job_id: str, src_path: str):
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS", "TRANSCRIBING"):
        return

    job["files"]["mp3"] = src_path
    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_transcription(job_id, src_path)


@app.task(name="tasks.scan_inbox_task")
def scan_inbox_task():
    scan_inbox()


def scan_inbox() -> int:
    os.makedirs(SCAN_DIR, exist_ok=True)
    tracked = {j.get("source_file") for j in read_jobs() if j["status"] != "DELETED"}
    count = 0
    for fname in sorted(os.listdir(SCAN_DIR)):
        if not fname.lower().endswith(".mp3"):
            continue
        if fname in tracked:
            continue
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "url": "",
            "title": os.path.splitext(fname)[0],
            "source_file": fname,
            "status": "PENDING",
            "progress": {},
            "files": {"mp3": None, "srt": None},
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        upsert_job(job)
        transcribe_file.apply_async(args=[job_id, os.path.join(SCAN_DIR, fname)], task_id=job_id)
        count += 1
    return count


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
