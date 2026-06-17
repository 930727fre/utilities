import asyncio
import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from annotate import annotate_executor, annotate_job
from gpu_lock import release_all_held
from srt_matcher import find_matching_srt
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from subs_finder import find_subs
from tasks import enumerate_playlist, executor, process_qb_file, process_video

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")

DOWNLOADS_DIR = Path("/app/data/downloads")
# qb mode scans only /qb. yt-tab files in DOWNLOADS_DIR show up in the yt
# tab's job list — no reason to also list them under qb.
QB_ROOTS = [Path("/qb")]
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}
ANNOTATION_MARKER = "※"
MTIME_GRACE_SECONDS = 60
QB_SCAN_INTERVAL = 30
MAX_ANNOTATION_RETRIES = 3
MAX_WHISPER_RETRIES = 3

# Per-path retry counters for the background loop. In-memory: container
# restart resets them, which is the natural "try again" signal.
_annotation_failures: dict[str, int] = {}
_whisper_failures: dict[str, int] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Hard-fail fast if the shared whisper service isn't reachable. Failing loudly
    # at startup beats silently dropping every whisper request later.
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{WHISPER_URL}/health")
            r.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"whisper service at {WHISPER_URL} not reachable at startup: {e}"
        ) from e

    ensure_jobs_file()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    # Any job mid-flight (or queued) at startup is orphaned from a prior crash.
    # Mark FAILED so the UI surfaces ! / ↻ instead of an eternal ○.
    jobs = read_jobs()
    changed = False
    for job in jobs:
        # Schema migration: source labels were renamed youtube→yt, library→qb.
        src = job.get("source")
        if src == "youtube":
            job["source"] = "yt"
            changed = True
        elif src == "library":
            job["source"] = "qb"
            changed = True
        if job["status"] in ("DOWNLOADING", "TRANSCRIBING", "PENDING"):
            job["status"] = "FAILED"
            job["error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned {job['job_id']} -> FAILED", flush=True)
        elif job["status"] == "ANNOTATING":
            # Annotation is optional; if it crashed mid-way, flip back to SUCCESS.
            # The .srt may be partially overwritten — the background loop will
            # pick it up again if it's a qb job, or the yt user re-runs the
            # whole job.
            job["status"] = "SUCCESS"
            job["annotation_error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned annotation {job['job_id']} -> SUCCESS", flush=True)
    if changed:
        write_jobs(jobs)

    annotation_loop_task = asyncio.create_task(_qb_work_loop())

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
        "source": "yt",
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


def _new_qb_job(job_id: str, source_path: str) -> dict:
    return {
        "job_id": job_id,
        "source": "qb",
        "source_path": source_path,
        "title": Path(source_path).name,
        "status": "PENDING",
        "annotated": False,
        "error": None,
        "annotation_error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


# ── yt API ─────────────────────────────────────────────────────────────────

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
async def list_jobs(source: str = "yt"):
    return [
        j for j in read_jobs()
        if j["status"] != "DELETED" and j.get("source", "yt") == source
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
    if job.get("source") == "qb":
        executor.submit(process_qb_file, job_id)
    else:
        executor.submit(process_video, job_id, job["url"])
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    # yt jobs: clean up the files we created. qb jobs reference files we
    # don't own — just drop the job entry, leave files alone.
    if job.get("source", "yt") == "yt":
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


# ── qb API ─────────────────────────────────────────────────────────────────

class QbTranscribeRequest(BaseModel):
    path: str


def _is_annotated_srt(srt_path: Path) -> bool:
    """`※` anywhere in the SRT means annotation already ran. Includes the
    99:59:59 sentinel cue appended even on 0-note passes, so this is a
    complete check — no need for a jobs.json overlay."""
    try:
        with open(srt_path, "rb") as f:
            content = f.read()
        return ANNOTATION_MARKER.encode("utf-8") in content
    except OSError:
        return False


def _scan_qb() -> list[dict]:
    """Walk QB_ROOTS, return one entry per video file. Filesystem only."""
    out = []
    now = time.time()
    for root in QB_ROOTS:
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


def _validate_qb_path(path_str: str) -> Path:
    """Reject paths outside the qb roots. Defense in depth."""
    path = Path(path_str).resolve()
    for root in QB_ROOTS:
        try:
            path.relative_to(root.resolve())
            return path
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail=f"Path not under a qb root: {path_str}")


@app.get("/api/qb")
async def qb():
    items = await asyncio.to_thread(_scan_qb)
    jobs = read_jobs()
    in_flight: dict[str, str] = {}
    for j in jobs:
        if j.get("source") != "qb":
            continue
        if j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING"):
            in_flight[j.get("source_path", "")] = j["job_id"]
    for item in items:
        item["in_flight_job_id"] = in_flight.get(item["path"])
        ann_fails = _annotation_failures.get(item["path"], 0)
        item["annotation_failures"] = ann_fails
        item["annotation_blocked"] = ann_fails >= MAX_ANNOTATION_RETRIES
        whisper_fails = _whisper_failures.get(item["path"], 0)
        item["whisper_failures"] = whisper_fails
        item["whisper_blocked"] = whisper_fails >= MAX_WHISPER_RETRIES
    return items


@app.post("/api/qb/transcribe", status_code=201)
async def transcribe_qb_file(req: QbTranscribeRequest):
    path = _validate_qb_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Reject duplicate in-flight requests for the same path.
    for j in read_jobs():
        if (j.get("source") == "qb"
                and j.get("source_path") == str(path)
                and j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING")):
            raise HTTPException(status_code=409, detail="Already in flight")

    job_id = str(uuid.uuid4())
    job = _new_qb_job(job_id, str(path))
    upsert_job(job)
    executor.submit(process_qb_file, job_id)
    return {"job_id": job_id, "status": "PENDING"}


# ── Background annotation loop ────────────────────────────────────────────

def _queue_pending_qb_work():
    """For every qb video without SRT, enqueue whisper; for every SRT without ※,
    enqueue annotation. Skips files already in flight and ones at the retry cap.
    """
    items = _scan_qb()
    jobs = read_jobs()
    in_flight_paths = set()
    for j in jobs:
        if j.get("source") != "qb":
            continue
        if j["status"] in ("PENDING", "DOWNLOADING", "TRANSCRIBING", "ANNOTATING"):
            in_flight_paths.add(j.get("source_path"))

    for item in items:
        if item["path"] in in_flight_paths:
            continue

        if not item["has_srt"]:
            video = Path(item["path"])

            # 1. Cheap: any sibling .srt the LLM matcher recognizes (release-
            #    bundled `.en.srt` etc.) — rename to satisfy strict match.
            matched = find_matching_srt(video)
            if matched is not None:
                target = video.with_suffix(".srt")
                try:
                    matched.rename(target)
                    print(f"[srt-matcher] {matched.name!r} → {target.name!r}", flush=True)
                except OSError as e:
                    print(f"[srt-matcher] rename failed ({e}); continuing", flush=True)
                else:
                    continue

            # 2. Try OpenSubtitles by file hash — human-translated subs are
            #    better than whisper output and free at the API layer (subject
            #    to daily quota; misses are cached in-memory to avoid retries).
            if find_subs(video) is not None:
                continue

            # 3. Fallback: GPU whisper.
            if _whisper_failures.get(item["path"], 0) >= MAX_WHISPER_RETRIES:
                continue
            job_id = str(uuid.uuid4())
            job = _new_qb_job(job_id, item["path"])
            upsert_job(job)
            executor.submit(_track_whisper_outcome, job_id, item["path"])
            continue

        # Has SRT — check if annotation is needed.
        if item["has_annotation"]:
            continue
        if _annotation_failures.get(item["path"], 0) >= MAX_ANNOTATION_RETRIES:
            continue
        job_id = str(uuid.uuid4())
        job = _new_qb_job(job_id, item["path"])
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


def _track_whisper_outcome(job_id: str, source_path: str):
    """Run whisper (and chained annotation); bump counter if whisper ends FAILED."""
    process_qb_file(job_id)  # @_catch_unhandled in tasks.py absorbs exceptions
    job = get_job(job_id)
    if job and job.get("status") == "FAILED":
        _whisper_failures[source_path] = _whisper_failures.get(source_path, 0) + 1
    else:
        _whisper_failures.pop(source_path, None)


async def _qb_work_loop():
    """Periodically scan /qb for files needing whisper or annotation, and queue them."""
    while True:
        try:
            await asyncio.to_thread(_queue_pending_qb_work)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(QB_SCAN_INTERVAL)
