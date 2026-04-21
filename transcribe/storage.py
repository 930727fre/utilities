import json
import os
from filelock import FileLock

JOBS_FILE = os.getenv("JOBS_FILE", "/app/data/jobs.json")
LOCK_FILE = JOBS_FILE + ".lock"
_lock = FileLock(LOCK_FILE)


def read_jobs() -> list:
    if not os.path.exists(JOBS_FILE):
        return []
    with _lock:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def write_jobs(jobs: list) -> None:
    with _lock:
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)


def get_job(job_id: str) -> dict | None:
    for job in read_jobs():
        if job["job_id"] == job_id:
            return job
    return None


def upsert_job(job: dict) -> None:
    with _lock:
        jobs = []
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                jobs = json.load(f)
        for i, j in enumerate(jobs):
            if j["job_id"] == job["job_id"]:
                jobs[i] = job
                break
        else:
            jobs.append(job)
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)


def ensure_jobs_file() -> None:
    os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
    if not os.path.exists(JOBS_FILE):
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
