import asyncio
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from annotate import annotate_executor, annotate_job
from gpu_lock import release_all_held
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from tasks import enumerate_playlist, executor, process_library_file, process_video

DOWNLOADS_DIR = Path("/app/data/downloads")
LIBRARY_ROOTS = [Path("/qb"), DOWNLOADS_DIR]
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}
ANNOTATION_MARKER = "※"
MTIME_GRACE_SECONDS = 60
LIBRARY_SCAN_INTERVAL = 30
MAX_ANNOTATION_RETRIES = 3

# Per-path retry counter for the background annotation loop. In-memory:
# container restart resets it, which is the natural "try again" signal.
_annotation_failures: dict[str, int] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_jobs_file()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    # Any job mid-flight (or queued) at startup is orphaned from a prior crash.
    # Mark FAILED so the UI surfaces ! / ↻ instead of an eternal ○.
    jobs = read_jobs()
    changed = False
    for job in jobs:
        if job["status"] in ("DOWNLOADING", "TRANSCRIBING", "PENDING"):
            job["status"] = "FAILED"
            job["error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned {job['job_id']} -> FAILED", flush=True)
        elif job["status"] == "ANNOTATING":
            # Annotation is optional; if it crashed mid-way, flip back to SUCCESS.
            # The .srt may be partially overwritten — the background loop will
            # pick it up again if it's a library job, or the YouTube user re-runs
            # the whole job.
            job["status"] = "SUCCESS"
            job["annotation_error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned annotation {job['job_id']} -> SUCCESS", flush=True)
    if changed:
        write_jobs(jobs)

    annotation_loop_task = asyncio.create_task(_library_annotation_loop())

    try:
        yield
    finally:
        annotation_loop_task.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        annotate_executor.shutdown(wait=False, cancel_futures=True)
        # Free any GPU leases still held — the broker has no TTL, so without
        # this a SIGTERM mid-whisper leaves the next acquire blocked forever.
        release_all_held()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


def _new_job(job_id: str, url: str) -> dict:
    return {
        "job_id": job_id,
        "source": "youtube",
        "url": url,
        "title": url,
        "status": "PENDING",
        "basename": None,
        "annotated": False,
        "error": None,
        "annotation_error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _new_library_job(job_id: str, source_path: str) -> dict:
    return {
        "job_id": job_id,
        "source": "library",
        "source_path": source_path,
        "title": Path(source_path).name,
        "status": "PENDING",
        "annotated": False,
        "error": None,
        "annotation_error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


# ── YouTube API ────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    url: str


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def _is_playlist_url(url: str) -> bool:
    """True only for canonical playlist URLs (`/playlist?list=…`).

    `watch?v=…&list=…` is intentionally NOT treated as a playlist — the user
    typically means the single video that happens to be inside one.
    """
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.netloc or "").lower().split(":", 1)[0]
    return host in _YT_HOSTS and u.path == "/playlist" and "list" in parse_qs(u.query)


@app.post("/api/jobs", status_code=201)
async def submit_job(req: SubmitRequest):
    if _is_playlist_url(req.url):
        try:
            entries = await asyncio.to_thread(enumerate_playlist, req.url)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Playlist enumeration failed: {e}")
        if not entries:
            raise HTTPException(status_code=400, detail="Playlist is empty or all entries unavailable")
        job_ids = []
        for entry in entries:
            job_id = str(uuid.uuid4())
            job = _new_job(job_id, entry["url"])
            if entry["title"]:
                job["title"] = entry["title"]
            upsert_job(job)
            executor.submit(process_video, job_id, entry["url"])
            job_ids.append(job_id)
        return {"playlist": True, "count": len(job_ids), "job_ids": job_ids}

    job_id = str(uuid.uuid4())
    job = _new_job(job_id, req.url)
    upsert_job(job)
    executor.submit(process_video, job_id, req.url)
    return {"job_id": job_id, "status": "PENDING"}


@app.get("/api/jobs")
async def list_jobs(source: str = "youtube"):
    return [
        j for j in read_jobs()
        if j["status"] != "DELETED" and j.get("source", "youtube") == source
    ]


@app.get("/api/jobs/{job_id}")
async def get_job_api(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        raise HTTPException(status_code=404, detail="Not found")
    return job


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] != "FAILED":
        raise HTTPException(status_code=400, detail="Job is not in FAILED state")
    job["status"] = "PENDING"
    job["error"] = None
    job["updated_at"] = _now()
    upsert_job(job)
    if job.get("source") == "library":
        executor.submit(process_library_file, job_id)
    else:
        executor.submit(process_video, job_id, job["url"])
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    # YouTube jobs: clean up the files we created. Library jobs reference
    # files we don't own — just drop the job entry, leave files alone.
    if job.get("source", "youtube") == "youtube":
        base = job.get("basename")
        if base:
            for ext in ("mp4", "srt"):
                (DOWNLOADS_DIR / f"{base}.{ext}").unlink(missing_ok=True)
        # In case the job died after download but before rename, also clean up
        # the staging UUID-named file.
        (DOWNLOADS_DIR / f"{job_id}.mp4").unlink(missing_ok=True)
        (DOWNLOADS_DIR / f"{job_id}.srt").unlink(missing_ok=True)

    job["status"] = "DELETED"
    job["updated_at"] = _now()
    upsert_job(job)
    return {"ok": True}


# ── Library API ────────────────────────────────────────────────────────────

class LibraryTranscribeRequest(BaseModel):
    path: str


def _is_annotated_srt(srt_path: Path) -> bool:
    """Annotation appends `※ <note>` lines; presence in head ≈ already sparkled."""
    try:
        with open(srt_path, "rb") as f:
            head = f.read(4096)
        return ANNOTATION_MARKER.encode("utf-8") in head
    except OSError:
        return False


def _youtube_staging_stems() -> set[str]:
    """Stems of UUID-named files still in flight as YouTube jobs — hide from Library."""
    return {
        j["job_id"] for j in read_jobs()
        if j.get("source", "youtube") == "youtube"
        and j["status"] in ("PENDING", "DOWNLOADING", "TRANSCRIBING", "ANNOTATING")
    }


def _scan_library(skip_stems: set[str] | None = None) -> list[dict]:
    """Walk LIBRARY_ROOTS, return one entry per video file. Filesystem only."""
    skip = skip_stems or set()
    out = []
    now = time.time()
    for root in LIBRARY_ROOTS:
        if not root.exists():
            continue
        for video in root.rglob("*"):
            if not video.is_file():
                continue
            if video.suffix.lower() not in VIDEO_EXTS:
                continue
            # qBittorrent marks downloads-in-progress with .!qB
            if video.name.endswith(".!qB"):
                continue
            # Optional qB layout: separate "incomplete" folder
            if "incomplete" in [p.lower() for p in video.parts]:
                continue
            if video.name.startswith("."):
                continue
            # In-flight YouTube staging file (still UUID-named pre-rename)
            if video.stem in skip:
                continue
            # File still being written? (e.g. just-finished rename)
            try:
                if now - video.stat().st_mtime < MTIME_GRACE_SECONDS:
                    continue
            except OSError:
                continue
            srt = video.with_suffix(".srt")
            has_srt = srt.exists()
            has_annotation = has_srt and _is_annotated_srt(srt)
            try:
                parent_rel = str(video.parent.relative_to(root))
            except ValueError:
                parent_rel = ""
            if parent_rel == ".":
                parent_rel = ""
            out.append({
                "path": str(video),
                "name": video.name,
                "parent": parent_rel,
                "root": str(root),
                "has_srt": has_srt,
                "has_annotation": has_annotation,
            })
    out.sort(key=lambda x: (x["root"], x["parent"], x["name"].lower()))
    return out


def _validate_library_path(path_str: str) -> Path:
    """Reject paths outside the library roots. Defense in depth."""
    path = Path(path_str).resolve()
    for root in LIBRARY_ROOTS:
        try:
            path.relative_to(root.resolve())
            return path
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail=f"Path not under a library root: {path_str}")


@app.get("/api/library")
async def library():
    skip = _youtube_staging_stems()
    items = await asyncio.to_thread(_scan_library, skip)
    jobs = read_jobs()
    in_flight: dict[str, str] = {}
    annotated_paths: set[str] = set()
    for j in jobs:
        if j.get("source") != "library":
            continue
        if j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING"):
            in_flight[j.get("source_path", "")] = j["job_id"]
        # Authoritative "annotated" record — handles the case where annotation
        # legitimately found zero notes to add and left the SRT without `※`.
        if j["status"] == "SUCCESS" and j.get("annotated") and j.get("source_path"):
            annotated_paths.add(j["source_path"])
    for item in items:
        if item["path"] in annotated_paths:
            item["has_annotation"] = True
        item["in_flight_job_id"] = in_flight.get(item["path"])
        failures = _annotation_failures.get(item["path"], 0)
        item["annotation_failures"] = failures
        item["annotation_blocked"] = failures >= MAX_ANNOTATION_RETRIES
    return items


@app.post("/api/library/transcribe", status_code=201)
async def transcribe_library_file(req: LibraryTranscribeRequest):
    path = _validate_library_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Reject duplicate in-flight requests for the same path.
    for j in read_jobs():
        if (j.get("source") == "library"
                and j.get("source_path") == str(path)
                and j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING")):
            raise HTTPException(status_code=409, detail="Already in flight")

    job_id = str(uuid.uuid4())
    job = _new_library_job(job_id, str(path))
    upsert_job(job)
    executor.submit(process_library_file, job_id)
    return {"job_id": job_id, "status": "PENDING"}


# ── Background annotation loop ────────────────────────────────────────────

def _queue_pending_library_annotations():
    """Scan library roots; for every SRT without ※, enqueue an annotation job.

    Skips files already in flight and files that have hit the per-path retry cap.
    """
    items = _scan_library(_youtube_staging_stems())
    jobs = read_jobs()
    in_flight_paths = set()
    annotated_paths = set()
    for j in jobs:
        if j.get("source") != "library":
            continue
        if j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING"):
            in_flight_paths.add(j.get("source_path"))
        if j["status"] == "SUCCESS" and j.get("annotated") and j.get("source_path"):
            annotated_paths.add(j["source_path"])
    for item in items:
        if not item["has_srt"]:
            continue
        if item["has_annotation"] or item["path"] in annotated_paths:
            continue
        if item["path"] in in_flight_paths:
            continue
        if _annotation_failures.get(item["path"], 0) >= MAX_ANNOTATION_RETRIES:
            continue
        job_id = str(uuid.uuid4())
        job = _new_library_job(job_id, item["path"])
        job["status"] = "ANNOTATING"
        upsert_job(job)
        annotate_executor.submit(_track_annotation_outcome, job_id, item["path"])


def _track_annotation_outcome(job_id: str, source_path: str):
    """Run annotation; bump in-memory failure counter on error."""
    try:
        annotate_job(job_id)
    except Exception:
        traceback.print_exc()
    job = get_job(job_id)
    if job and job.get("annotation_error"):
        _annotation_failures[source_path] = _annotation_failures.get(source_path, 0) + 1
    else:
        _annotation_failures.pop(source_path, None)


async def _library_annotation_loop():
    """Periodically scan for unannotated SRTs and queue them."""
    while True:
        try:
            await asyncio.to_thread(_queue_pending_library_annotations)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(LIBRARY_SCAN_INTERVAL)
