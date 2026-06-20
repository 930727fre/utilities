import asyncio
import os
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import bt_torrents
from annotate import annotate_executor, annotate_job
from gpu_lock import release_all_held
from srt_matcher import find_matching_srt
from srt_source import stamp_source
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from subs_finder import find_subs
from translator import translate_video_zh, translator_executor
from tasks import enumerate_playlist, executor, process_bt_file, process_video

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")

DOWNLOADS_DIR = Path("/app/data/downloads")
# bt mode scans only /bt. yt-tab files in DOWNLOADS_DIR show up in the yt
# tab's job list — no reason to also list them under bt.
BT_ROOTS = [Path("/bt")]
# When the user clicks the "translate to zh" button on a bt torrent, we
# write the Chinese sub as a sidecar next to the video — same folder, same
# stem, .zh-tw.srt suffix. Infuse picks it up as a separate language track.
ZH_LANGUAGES = "zh-tw,zh-cn"
ZH_SUFFIX = ".zh-tw.srt"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}
ANNOTATION_MARKER = "※ annotated"
WHISPER_FAILED_MARKER = "※ whisper failed:"
ANNOTATE_FAILED_MARKER = "※ annotate failed:"
MTIME_GRACE_SECONDS = 60
BT_SCAN_INTERVAL = 30

# No in-memory failure counters: "we tried this once" is recorded as a
# sentinel cue inside the SRT itself (see srt_source.stamp_*), so the
# scan loop reads it back on every tick and survives container restarts.
# Auto-retry is gone; the UI ↻ button is the only path back into the
# pipeline for a failed file.

# In-flight translate-to-zh futures keyed by absolute video path. Set when
# the translate-zh endpoint submits a worker, cleared (via a `finally` in
# the submission wrapper) when the worker exits. The bt scan reads this
# to expose `zh_in_flight` per video so the UI knows the "中" pulse is
# alive — and the per-torrent "→ 中" button can disable while any of its
# videos are still being translated. Container restart wipes the dict
# along with the executor's threads, which is the right behaviour: any
# half-finished translations get retried by the next button click.
_translating: dict[str, Future] = {}
_translating_lock = threading.Lock()


def _submit_translate(video: Path) -> None:
    def _run():
        try:
            translate_video_zh(video)
        finally:
            with _translating_lock:
                _translating.pop(str(video), None)

    fut = translator_executor.submit(_run)
    with _translating_lock:
        _translating[str(video)] = fut


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
        # Schema migration: source labels were renamed youtube→yt, library→qb→bt.
        src = job.get("source")
        if src == "youtube":
            job["source"] = "yt"
            changed = True
        elif src in ("library", "qb"):
            job["source"] = "bt"
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
            # pick it up again if it's a bt job, or the yt user re-runs the
            # whole job.
            job["status"] = "SUCCESS"
            job["annotation_error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned annotation {job['job_id']} -> SUCCESS", flush=True)
    if changed:
        write_jobs(jobs)

    # Re-spawn aria2c for any wrapper that has a half-finished download
    # (a `.aria2` control file present from before the container restart).
    bt_torrents.resume_all()

    annotation_loop_task = asyncio.create_task(_bt_work_loop())

    try:
        yield
    finally:
        annotation_loop_task.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        annotate_executor.shutdown(wait=False, cancel_futures=True)
        translator_executor.shutdown(wait=False, cancel_futures=True)
        bt_torrents.shutdown()
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


def _new_bt_job(job_id: str, source_path: str) -> dict:
    return {
        "job_id": job_id,
        "source": "bt",
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
    if job.get("source") == "bt":
        executor.submit(process_bt_file, job_id)
    else:
        executor.submit(process_video, job_id, job["url"])
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    if job.get("source", "yt") == "yt":
        # Clean up the mp4 + srt we put under data/downloads/.
        base = job.get("basename")
        if base:
            for ext in ("mp4", "srt"):
                (DOWNLOADS_DIR / f"{base}.{ext}").unlink(missing_ok=True)
        # And the staging UUID-named files in case the job died mid-rename.
        (DOWNLOADS_DIR / f"{job_id}.mp4").unlink(missing_ok=True)
        (DOWNLOADS_DIR / f"{job_id}.srt").unlink(missing_ok=True)

    # bt jobs reference files transcribe doesn't own through jobs.json (the
    # legacy /api/bt/transcribe path) or are stale magnet-era entries from
    # earlier schema versions. Either way: just drop the entry, leave the
    # filesystem alone. Torrents are managed via /api/bt/torrent/{wrapper}.

    job["status"] = "DELETED"
    job["updated_at"] = _now()
    upsert_job(job)
    return {"ok": True}


# ── bt API ─────────────────────────────────────────────────────────────────

class BtTranscribeRequest(BaseModel):
    path: str


def _is_annotated_srt(srt_path: Path) -> bool:
    """`※ annotated` anywhere in the SRT means annotation already ran.
    Includes the 99:59:59 sentinel cue appended even on 0-note passes, so
    this is a complete check — no need for a jobs.json overlay. (Producer
    source sentinels use `※ source: …` which won't false-trigger this.)"""
    try:
        with open(srt_path, "rb") as f:
            content = f.read()
        return ANNOTATION_MARKER.encode("utf-8") in content
    except OSError:
        return False


def _read_srt_marker(srt_path: Path, marker: str) -> str | None:
    """If the SRT contains a sentinel cue starting with `marker`, return the
    text that follows (the error message). Returns None if not present."""
    try:
        content = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in content.splitlines():
        idx = line.find(marker)
        if idx >= 0:
            return line[idx + len(marker):].strip() or "(no message)"
    return None


def _scan_bt() -> list[dict]:
    """Walk BT_ROOTS, return one entry per video file. Filesystem only,
    plus the in-flight set of translate-zh futures for `zh_in_flight`."""
    out = []
    now = time.time()
    with _translating_lock:
        in_flight = {p for p, f in _translating.items() if not f.done()}
    for root in BT_ROOTS:
        if not root.exists():
            continue
        # aria2c keeps ONE `.aria2` control file per torrent (not one per
        # file inside a multi-file torrent), and only deletes it once every
        # piece across every file has verified. So any `.aria2` anywhere
        # under a wrapper means EVERY file in that wrapper is still being
        # written / verified and unsafe to scan — far more reliable than
        # the mtime check below for stalled-peer cases where individual
        # file writes can pause for minutes.
        wrappers_in_flight = {
            w.resolve()
            for w in root.iterdir()
            if w.is_dir() and any(w.rglob("*.aria2"))
        }
        for video in root.rglob("*"):
            if not video.is_file():
                continue
            if video.suffix.lower() not in VIDEO_EXTS:
                continue
            if video.name.startswith("."):
                continue
            # Skip the whole wrapper if its torrent hasn't finished yet.
            try:
                wrapper = root / video.relative_to(root).parts[0]
                if wrapper.resolve() in wrappers_in_flight:
                    continue
            except (ValueError, IndexError):
                # video is directly at root with no wrapper; nothing to skip.
                pass
            # Belt-and-suspenders for non-aria2c writers (manual drops,
            # webdav copies, rsync mid-flight): files whose mtime hasn't
            # settled for 60 s aren't ready yet.
            try:
                if now - video.stat().st_mtime < MTIME_GRACE_SECONDS:
                    continue
            except OSError:
                continue
            srt = video.with_suffix(".srt")
            has_srt = srt.exists()
            whisper_error = _read_srt_marker(srt, WHISPER_FAILED_MARKER) if has_srt else None
            annotate_error = _read_srt_marker(srt, ANNOTATE_FAILED_MARKER) if has_srt else None
            # An SRT carrying only the whisper-failed sentinel isn't a real
            # transcript — for everything downstream of whisper we treat it
            # as "no SRT yet, but don't retry."
            has_real_srt = has_srt and whisper_error is None
            has_annotation = has_real_srt and _is_annotated_srt(srt)
            # Chinese sub state (user-triggered translate-zh button output).
            # `.zh-tw.srt` is the sidecar; `.zh-tw.srt.error` records a
            # failure reason from OS-miss + Gemini-also-failed paths.
            zh_path = video.parent / f"{video.stem}{ZH_SUFFIX}"
            zh_err_path = Path(str(zh_path) + ".error")
            has_zh_srt = zh_path.exists()
            zh_in_flight = str(video) in in_flight
            zh_error: str | None = None
            if not has_zh_srt and zh_err_path.exists():
                try:
                    zh_error = zh_err_path.read_text(encoding="utf-8", errors="replace").strip() or "(empty error)"
                except OSError:
                    zh_error = "(unreadable .zh-tw.srt.error stamp)"
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
                "has_srt": has_real_srt,
                "has_annotation": has_annotation,
                "whisper_error": whisper_error,
                "annotate_error": annotate_error,
                "has_zh_srt": has_zh_srt,
                "zh_in_flight": zh_in_flight,
                "zh_error": zh_error,
            })
    out.sort(key=lambda x: (x["root"], x["parent"], x["name"].lower()))
    return out


def _validate_bt_path(path_str: str) -> Path:
    """Reject paths outside the bt roots. Defense in depth."""
    path = Path(path_str).resolve()
    for root in BT_ROOTS:
        try:
            path.relative_to(root.resolve())
            return path
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail=f"Path not under a bt root: {path_str}")


@app.get("/api/bt")
async def bt():
    items = await asyncio.to_thread(_scan_bt)
    jobs = read_jobs()
    in_flight: dict[str, str] = {}
    for j in jobs:
        if j.get("source") != "bt":
            continue
        if j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING"):
            in_flight[j.get("source_path", "")] = j["job_id"]
    for item in items:
        item["in_flight_job_id"] = in_flight.get(item["path"])
    return items


class BtMagnetRequest(BaseModel):
    magnet: str


@app.post("/api/bt/magnet", status_code=201)
async def submit_magnet(req: BtMagnetRequest):
    """Spawn a one-shot aria2c subprocess for this magnet. The torrent
    lives entirely in the subprocess's lifetime + the per-torrent wrapper
    folder under /bt; nothing is persisted in jobs.json."""
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        wrapper = bt_torrents.submit(req.magnet)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"aria2c launch failed: {exc}")
    return {"wrapper": wrapper}


@app.get("/api/bt/torrents")
async def list_torrents():
    """One entry per wrapper folder under /bt, phase derived live from
    aria2c's `.aria2` control file + the subprocess registry."""
    return bt_torrents.list_torrents()


@app.delete("/api/bt/torrents/{wrapper}")
async def delete_torrent(wrapper: str):
    """Kill the subprocess (if running) + rmtree the wrapper folder."""
    bt_torrents.delete(wrapper)
    return {"ok": True}


class BtTranslateZhRequest(BaseModel):
    wrapper: str


@app.post("/api/bt/translate-zh", status_code=200)
async def bt_translate_zh(req: BtTranslateZhRequest):
    """Submit every video in `wrapper` to the Chinese-translation worker.
    Each video tries OS find_subs(zh-tw,zh-cn) first, falls through to
    Gemini translating the sibling `<stem>.srt`, writes `<stem>.zh-tw.srt`
    on success or `<stem>.zh-tw.srt.error` on failure.

    Refuses if:
      - the wrapper isn't directly under /bt
      - the torrent hasn't finished (any `.aria2` anywhere)
      - any video lacks the `※ annotated` sentinel (i.e. bt pipeline not
        done yet — without an annotated English SRT, the Gemini fallback
        has nothing to translate from)

    Idempotent — videos that already have `<stem>.zh-tw.srt` get skipped
    inside the worker. Calling again after a partial failure clears
    `.error` stamps + the subs_finder cache, so the button doubles as
    retry."""
    bt_root = BT_ROOTS[0]
    src = (bt_root / req.wrapper).resolve()
    try:
        src.relative_to(bt_root.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="wrapper not under /bt")
    if not src.is_dir():
        raise HTTPException(status_code=404, detail=f"wrapper not found: {req.wrapper}")
    if any(src.rglob("*.aria2")):
        raise HTTPException(status_code=409, detail="torrent still downloading")

    videos: list[Path] = []
    for video in src.rglob("*"):
        if not video.is_file():
            continue
        if video.suffix.lower() not in VIDEO_EXTS:
            continue
        if video.name.startswith("."):
            continue
        srt = video.with_suffix(".srt")
        if not srt.exists():
            raise HTTPException(status_code=409, detail=f"{video.name} has no SRT yet")
        try:
            if ANNOTATION_MARKER.encode("utf-8") not in srt.read_bytes():
                raise HTTPException(status_code=409, detail=f"{video.name} not annotated yet")
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"reading SRT failed: {e}")
        videos.append(video)
    # rglob order is filesystem-dependent (inode order on ext4). Sort by
    # name so a multi-episode pack like Sopranos S01 translates E01→E13 in
    # order rather than whatever shape the seeder happened to land bytes in.
    videos.sort(key=lambda p: p.name)

    queued = 0
    with _translating_lock:
        in_flight = {p for p, f in _translating.items() if not f.done()}
    for video in videos:
        zh_path = video.parent / f"{video.stem}{ZH_SUFFIX}"
        if zh_path.exists():
            continue  # already translated
        if str(video) in in_flight:
            continue  # already submitted, worker hasn't finished
        # Clear stale failure state so retry-via-button works as expected.
        err_path = Path(str(zh_path) + ".error")
        if err_path.exists():
            try:
                err_path.unlink()
            except OSError:
                pass
        _submit_translate(video)
        queued += 1
    return {"ok": True, "queued": queued, "total": len(videos)}


class BtRetryRequest(BaseModel):
    path: str


@app.post("/api/bt/retry", status_code=200)
async def bt_retry(req: BtRetryRequest):
    """Clear a failure sentinel so the scan loop picks the file up again.

    Implementation: delete the SRT entirely. Yes that throws away a
    successfully-transcribed transcript if the failure was only on the
    annotation stage — but that's the trade-off for not having three
    different retry buttons. Whisper's the expensive bit, and even then
    it's <10 min on the GPU; cheaper than building partial-retry UI.
    """
    path = _validate_bt_path(req.path)
    srt = path.with_suffix(".srt")
    if srt.exists():
        try:
            srt.unlink()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"unlink failed: {e}")
    return {"ok": True}


@app.post("/api/bt/transcribe", status_code=201)
async def transcribe_bt_file(req: BtTranscribeRequest):
    path = _validate_bt_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Reject duplicate in-flight requests for the same path.
    for j in read_jobs():
        if (j.get("source") == "bt"
                and j.get("source_path") == str(path)
                and j["status"] in ("PENDING", "TRANSCRIBING", "ANNOTATING")):
            raise HTTPException(status_code=409, detail="Already in flight")

    job_id = str(uuid.uuid4())
    job = _new_bt_job(job_id, str(path))
    upsert_job(job)
    executor.submit(process_bt_file, job_id)
    return {"job_id": job_id, "status": "PENDING"}


# ── Background annotation loop ────────────────────────────────────────────

def _queue_pending_bt_work():
    """For every bt video without SRT, enqueue whisper; for every SRT without ※,
    enqueue annotation. Files carrying a `※ whisper failed:` or
    `※ annotate failed:` sentinel in their SRT are skipped — the worker
    that hit the error wrote the sentinel itself, and the user clears it
    via the UI ↻ button (which deletes the SRT).
    """
    items = _scan_bt()
    jobs = read_jobs()
    in_flight_paths = set()
    for j in jobs:
        if j.get("source") != "bt":
            continue
        if j["status"] in ("PENDING", "DOWNLOADING", "TRANSCRIBING", "ANNOTATING"):
            in_flight_paths.add(j.get("source_path"))

    for item in items:
        if item["path"] in in_flight_paths:
            continue

        # whisper-failed SRTs read as `has_srt=False` from _scan_bt, but the
        # sentinel is still present on disk — skip so we don't re-fire.
        if item["whisper_error"]:
            continue

        if not item["has_srt"]:
            video = Path(item["path"])

            # 1. Cheap: any sibling .srt the LLM matcher recognizes (release-
            #    bundled `.en.srt`, RARBG's `Subs/<stem>/N_English.srt`, etc.).
            #    COPY (not move) so the torrent's original layout — and other
            #    language tracks shipped alongside — stay intact. stage_tag
            #    is "bundled-flat" or "bundled-agent" — stamped into the SRT
            #    so the user can tell which path produced this transcript.
            match_result = find_matching_srt(video)
            if match_result is not None:
                matched, stage_tag = match_result
                target = video.with_suffix(".srt")
                try:
                    shutil.copy2(matched, target)
                    stamp_source(target, stage_tag)
                    print(f"[srt-matcher] {matched.relative_to(video.parent)!s} → {target.name!r} ({stage_tag})", flush=True)
                except OSError as e:
                    print(f"[srt-matcher] copy/stamp failed ({e}); continuing", flush=True)
                else:
                    continue

            # 2. Try OpenSubtitles by file hash — human-translated subs are
            #    better than whisper output and free at the API layer (subject
            #    to daily quota; misses are cached in-memory to avoid retries).
            if find_subs(video) is not None:
                continue

            # 3. Fallback: GPU whisper. If it fails, tasks.py stamps the
            #    failure sentinel onto the SRT itself — we'll see it on the
            #    next tick and skip via the `whisper_error` branch above.
            job_id = str(uuid.uuid4())
            job = _new_bt_job(job_id, item["path"])
            upsert_job(job)
            executor.submit(process_bt_file, job_id)
            continue

        # Has SRT — check if annotation is needed. Skip annotated-once-
        # failed cases via the sentinel.
        if item["has_annotation"]:
            continue
        if item["annotate_error"]:
            continue
        job_id = str(uuid.uuid4())
        job = _new_bt_job(job_id, item["path"])
        job["status"] = "ANNOTATING"
        upsert_job(job)
        annotate_executor.submit(annotate_job, job_id)


async def _bt_work_loop():
    """Periodically scan /bt for files needing whisper or annotation, and queue them."""
    while True:
        try:
            await asyncio.to_thread(_queue_pending_bt_work)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(BT_SCAN_INTERVAL)


