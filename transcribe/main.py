import asyncio
import os
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

from bt_filter import (
    ARTIFACT_ROOT,
    BT_ROOT,
    _sentinel_for as bt_filter_sentinel_for,
    _sources_path,
    filter_wrapper,
    load_manifest,
)
from gpu_lock import release_all_held
from srt_source import (
    annotate_failed_path,
    read_failure_reason,
    whisper_failed_path,
    whisper_polluted_path,
)
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from translator import translate_video_zh, translator_executor
from tasks import enumerate_playlist, executor, process_bt_file, process_video

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")
# aria2 subprocess management lives in a separate container so BT traffic
# routes through PIA gluetun VPN while all other transcribe API calls
# stay on my_network direct. See utilities/aria2/ for the service.
ARIA2_URL = os.environ.get("ARIA2_URL", "http://aria2-gluetun:8080")
_aria2_client = httpx.Client(base_url=ARIA2_URL, timeout=30.0)

DOWNLOADS_DIR = Path("/app/data/downloads")
# bt mode scans only /app/data/artifact. yt-tab files in DOWNLOADS_DIR
# show up in the yt tab's job list — no reason to also list them under bt.
BT_ROOTS = [Path("/app/data/artifact")]
# When the user clicks the "translate to zh" button on a bt torrent, we
# write the Chinese sub as a sidecar next to the video — same folder, same
# stem, .zh-tw.srt suffix. Infuse picks it up as a separate language track.
ZH_SUFFIX = ".zh-tw.srt"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}
MTIME_GRACE_SECONDS = 60
BT_SCAN_INTERVAL = 30

# Master switch for the automatic scan loop that runs bt_filter (LLM
# classification + hardlink to /artifact) and queues the downstream
# whisper / annotation pipeline. When 0, both auto-loops early-return
# and the container becomes a pure aria2 harness: torrents download and
# seed as usual, but nothing else happens. Manual endpoints
# (`/api/bt/transcribe`, `/api/bt/retry`, `/api/bt/translate-zh`,
# `/api/bt/upgrade-english`) still work — user-triggered actions
# deliberately bypass the switch since they express explicit intent.
# Default 1 (normal behavior). Set 0 for a "download-only" period.
BT_PIPELINE_ENABLED = os.environ.get("BT_PIPELINE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off", "")

# State model: file existence alone — no marker reads, no jobs.json
# overlay. canonical SRT exists = the pipeline finished verifying +
# annotating; <stem>.whisper-failed sidecar = pipeline halted at
# whisper; <stem>.whisper-polluted sidecar = whisper produced a
# hallucination loop and no candidate was available to substitute;
# <stem>.annotate-failed sidecar = pipeline halted at annotation. The
# ↻ button deletes canonical + every sidecar to let the scan loop
# re-queue the pipeline; cached `_sources/` candidates are kept so
# replay only re-does the stages that were missing.

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
            # Annotation is now inline inside the pipeline; an interrupted
            # ANNOTATING job means the pipeline died mid-Sonnet-pass and
            # the canonical SRT was never atomically written. For bt jobs
            # the next scan tick will re-queue process_bt_file and replay
            # only the annotation step (verified.srt is cached). For yt
            # jobs the user re-submits the URL. Either way: FAILED is the
            # honest status.
            job["status"] = "FAILED"
            job["error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned annotation {job['job_id']} -> FAILED", flush=True)
    if changed:
        write_jobs(jobs)

    # aria2 subprocess lifecycle (resume-in-flight, kill-on-shutdown)
    # lives in the utilities/aria2 sidecar container now — its own
    # FastAPI lifespan handles resume_all() at startup and shutdown()
    # at teardown. Nothing for us to do here.

    annotation_loop_task = asyncio.create_task(_bt_work_loop())

    try:
        yield
    finally:
        annotation_loop_task.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        translator_executor.shutdown(wait=False, cancel_futures=True)
        _aria2_client.close()
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


def _bt_inode_map() -> dict[int, str]:
    """Build {inode → bt wrapper name} by walking /bt once.

    Canonical videos in /artifact share inodes with their bt-side
    originals (bt_filter uses `os.link`), so an inode match is a
    byte-level identity between the two paths. Called once per
    `_scan_bt` invocation and consulted per-video for O(1) wrapper
    attribution — beats calling `_find_bt_wrapper` per video, which
    would rewalk /bt each time.

    Empty map if /bt doesn't exist or is empty; per-video code treats
    missing wrapper as "unknown" without failing."""
    out: dict[int, str] = {}
    if not BT_ROOT.exists():
        return out
    for wrapper in BT_ROOT.iterdir():
        if not wrapper.is_dir():
            continue
        for entry in wrapper.rglob("*"):
            try:
                if entry.is_file():
                    out[entry.stat().st_ino] = wrapper.name
            except OSError:
                continue
    return out


def _scan_bt() -> list[dict]:
    """Walk BT_ROOTS, return one entry per video file. Filesystem only,
    plus the in-flight set of translate-zh futures for `zh_in_flight`.

    Each item carries a `wrapper` field naming the bt-side torrent
    wrapper it was hardlinked from (by inode match); the frontend uses
    it to group /artifact items under their originating torrent card
    for per-torrent action buttons (`→ 中`, `→ E`). Empty string when
    the bt-side original has been removed (orphaned canonical) or was
    manually dropped without going through bt_filter."""
    out = []
    now = time.time()
    with _translating_lock:
        in_flight = {p for p, f in _translating.items() if not f.done()}
    inode_to_wrapper = _bt_inode_map()
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
            # Failure sidecars next to the video signal "pipeline tried,
            # don't retry until user clears via the ↻ button". Filename
            # is the state — extension-less so Jellyfin / Infuse never
            # try to load them as subtitles. Canonical SRT existence
            # alone is the "fully done" signal — no marker reads.
            whisper_error = read_failure_reason(whisper_failed_path(video))
            whisper_polluted_error = read_failure_reason(whisper_polluted_path(video))
            annotate_error = read_failure_reason(annotate_failed_path(video))
            # Chinese sub state (user-triggered translate-zh button output).
            # `.zh-tw.srt` is the sidecar; `.zh-tw.srt.error` records a
            # failure reason from the Chinese translator when it surfaces one.
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
            try:
                wrapper_name = inode_to_wrapper.get(video.stat().st_ino, "")
            except OSError:
                wrapper_name = ""
            out.append({
                "path": str(video),
                "name": video.name,
                "parent": parent_rel,
                "wrapper": wrapper_name,
                "root": str(root),
                "has_srt": has_srt,
                "whisper_error": whisper_error,
                "whisper_polluted_error": whisper_polluted_error,
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
    """Proxy to the aria2 sidecar's POST /torrents so BT traffic exits
    through gluetun's VPN tunnel. Response body is passed through
    verbatim (currently `{"wrapper": "..."}`)."""
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        r = _aria2_client.post("/torrents", json={"magnet": req.magnet})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"aria2 sidecar unreachable: {exc}")
    if r.status_code >= 400:
        detail = _extract_detail(r) or f"aria2 sidecar returned {r.status_code}"
        raise HTTPException(status_code=r.status_code, detail=detail)
    return r.json()


@app.get("/api/bt/torrents")
async def list_torrents():
    """Proxy the aria2 sidecar's GET /torrents. Response body is
    passed through verbatim (list of `{name, phase, progress?}`)."""
    try:
        r = _aria2_client.get("/torrents")
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"aria2 sidecar unreachable: {exc}")
    return r.json()


def _extract_detail(r: httpx.Response) -> str | None:
    """Try to lift `detail` out of an httpx JSON error response body,
    falling back to raw text (truncated)."""
    try:
        body = r.json()
    except ValueError:
        return (r.text or "")[:300] or None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
    return None


# Top-level dirs under /artifact that the cleanup walk must NOT delete
# even if they happen to become empty. Anything else (Movie title dirs,
# Season dirs, _sources mirror dirs) is fair game to rmdir on bottom-up
# sweep.
_PRESERVED_DIR_NAMES = {"Movies", "TV", "_sources", "_processed"}


def _enumerate_deletion_targets(wrapper: str) -> dict:
    """Walk filesystem for everything a `delete_torrent(wrapper)` would
    actually remove. Returns a dict suitable for both the preview
    endpoint (dry-run UI) and the actual delete handler (which uses the
    same enumeration to do the work).

    Only existing files are listed; missing entries are silently skipped.
    `bt_size_bytes` is the sum of file sizes under `/bt/<wrapper>/` —
    canonical /artifact videos share inodes with these via hardlink, so
    bt's total is the disk that actually frees up when both sides go."""
    canonical_videos = load_manifest(wrapper)

    bt_wrapper = BT_ROOT / wrapper
    bt_files: list[Path] = []
    bt_size = 0
    if bt_wrapper.is_dir():
        for entry in sorted(bt_wrapper.rglob("*")):
            if entry.is_file():
                bt_files.append(entry)
                try:
                    bt_size += entry.stat().st_size
                except OSError:
                    pass

    # Glob `<stem>.*` covers every pipeline-written file next to a
    # canonical video — the .mkv/.mp4 itself, .srt, .zh-tw.srt[.error],
    # .whisper-failed, .whisper-polluted, .annotate-failed. Literal `.`
    # after stem prevents prefix collisions (e.g. `S01E01a.mkv` vs
    # `S01E01.mkv`). New sidecar types added later are covered for free.
    # Same idea in _sources/ — bounded by `.srt` suffix to stay strictly
    # inside the candidate-cache set.
    canonical_files: list[Path] = []
    sources_files: list[Path] = []
    for video in canonical_videos:
        if video.parent.is_dir():
            for f in sorted(video.parent.glob(f"{video.stem}.*")):
                if f.is_file():
                    canonical_files.append(f)
        sources_dir = _sources_path(video, "x").parent  # tag arbitrary; dir same
        if sources_dir.is_dir():
            for f in sorted(sources_dir.glob(f"{video.stem}.*.srt")):
                if f.is_file():
                    sources_files.append(f)

    sentinel = bt_filter_sentinel_for(wrapper)

    return {
        "canonical_videos": canonical_videos,  # used internally by delete
        "bt_wrapper": bt_wrapper if bt_wrapper.is_dir() else None,
        "bt_files": bt_files,
        "bt_size_bytes": bt_size,
        "canonical_files": sorted(canonical_files),
        "sources_files": sorted(sources_files),
        "sentinel": sentinel if sentinel.is_file() else None,
    }


@app.get("/api/bt/torrents/{wrapper}/preview-delete")
async def preview_delete_torrent(wrapper: str):
    """Dry-run: list every file the cascade-delete would remove plus
    a disk-recovery estimate. Frontend renders this verbatim in the
    confirm dialog so the user sees the exact damage before clicking."""
    plan = _enumerate_deletion_targets(wrapper)
    return {
        "bt_wrapper": str(plan["bt_wrapper"]) if plan["bt_wrapper"] else None,
        "bt_files": [str(p) for p in plan["bt_files"]],
        "bt_size_bytes": plan["bt_size_bytes"],
        "canonical_files": [str(p) for p in plan["canonical_files"]],
        "sources_files": [str(p) for p in plan["sources_files"]],
        "sentinel": str(plan["sentinel"]) if plan["sentinel"] else None,
    }


def _rmdir_empty_walk_up(start: Path) -> None:
    """Bottom-up rmdir of empty dirs from `start` upwards, bounded to
    inside ARTIFACT_ROOT and stopping at preserved top-level names.

    Refuses to walk if `start` resolves outside ARTIFACT_ROOT — paranoid
    safety against ever rmdir-ing something outside the library."""
    try:
        start.resolve().relative_to(ARTIFACT_ROOT.resolve())
    except ValueError:
        return
    p = start
    while p.is_dir() and p.name not in _PRESERVED_DIR_NAMES:
        try:
            p.rmdir()
        except OSError:
            break  # not empty / permission / race; stop here
        p = p.parent


@app.delete("/api/bt/torrents/{wrapper}")
async def delete_torrent(wrapper: str):
    """Cascade-delete a torrent: aria2 subprocess + /bt/<wrapper>/ +
    every canonical /artifact entry the torrent produced (videos, all
    SRT sidecars, failure sidecars) + cached _sources/ files + the
    .filtered sentinel.

    Refuses (409) if any video in this wrapper has a pipeline job
    in-flight — half-deleting while the worker writes canonical leads
    to ugly partial state. User should wait or cancel the job first.

    Shares enumeration with /preview-delete so the frontend dialog can
    show the exact path list before this fires."""
    plan = _enumerate_deletion_targets(wrapper)
    canonical_videos: list[Path] = plan["canonical_videos"]

    # Refuse if any pipeline job is in-flight for this wrapper's videos.
    in_flight_paths = {
        j["source_path"] for j in read_jobs()
        if j.get("source") == "bt"
        and j.get("source_path")
        and j["status"] in ("PENDING", "DOWNLOADING", "TRANSCRIBING", "ANNOTATING")
    }
    blocked = [v for v in canonical_videos if str(v) in in_flight_paths]
    if blocked:
        names = ", ".join(v.name for v in blocked[:3])
        raise HTTPException(
            status_code=409,
            detail=f"{len(blocked)} video(s) mid-pipeline ({names}…); "
                   f"wait for finish or delete the job first",
        )

    # Kill aria2 subprocess (via the sidecar) + rmtree /bt/<wrapper>/.
    # Sidecar failure isn't fatal: canonical/_sources/sentinel cleanup
    # below still runs, and the wrapper stays for the user to inspect.
    try:
        r = _aria2_client.delete(f"/torrents/{wrapper}")
        if r.status_code >= 400 and r.status_code != 404:
            print(f"[delete_torrent] aria2 sidecar returned {r.status_code} "
                  f"for {wrapper!r}: {r.text[:200]}", flush=True)
    except httpx.HTTPError as exc:
        print(f"[delete_torrent] aria2 sidecar unreachable while deleting "
              f"{wrapper!r}: {exc}", flush=True)

    # Unlink everything the plan listed (canonical + _sources/ + sentinel).
    for p in plan["canonical_files"] + plan["sources_files"]:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    if plan["sentinel"] is not None:
        try:
            plan["sentinel"].unlink(missing_ok=True)
        except OSError:
            pass

    # Bottom-up sweep: empty Season/Show/_sources-mirror dirs.
    swept: set[Path] = set()
    for video in canonical_videos:
        swept.add(video.parent)
        swept.add(_sources_path(video, "x").parent)  # tag arbitrary; dir same
    for d in swept:
        _rmdir_empty_walk_up(d)

    return {"ok": True, "videos_removed": len(canonical_videos)}


class BtTranslateZhRequest(BaseModel):
    wrapper: str


class BtUpgradeEnglishRequest(BaseModel):
    wrapper: str


@app.post("/api/bt/upgrade-english", status_code=200)
async def bt_upgrade_english(req: BtUpgradeEnglishRequest):
    """Force a fresh OpenSubtitles attempt for every video in `wrapper`.

    Deletes cached OS candidates (`_sources/<stem>.opensubtitles-hash.srt`
    and `…-text.srt`) so the pipeline re-fetches against today's quota,
    then deletes the canonical SRT so the pipeline actually re-runs.
    The cached whisper SRT in `_sources/` is left alone — no GPU re-pass
    needed; the verifier will replay against the same ground-truth
    transcript.

    Trade-off: any annotation work on affected videos is lost (Sonnet
    pass re-runs after the new SRT lands). Run this only when quota has
    reset and a fresh OS hit is genuinely valuable. Skips videos already
    in flight.

    `wrapper` is the bt-side wrapper name — videos resolve via the
    canonical-path manifest written by bt_filter."""
    if any((BT_ROOT / req.wrapper).rglob("*.aria2")):
        raise HTTPException(status_code=409, detail="torrent still downloading")
    canonical_videos = load_manifest(req.wrapper)
    if not canonical_videos:
        raise HTTPException(
            status_code=404,
            detail=f"no artifact manifest for wrapper: {req.wrapper}",
        )

    in_flight_paths = set()
    for j in read_jobs():
        if j.get("source") != "bt":
            continue
        if j["status"] in ("PENDING", "DOWNLOADING", "TRANSCRIBING", "ANNOTATING"):
            in_flight_paths.add(j.get("source_path"))

    cleared = 0
    for video in canonical_videos:
        if not video.is_file():
            continue
        if str(video) in in_flight_paths:
            continue
        # Drop cached OS candidates so the next pipeline pass refetches.
        for tag in ("opensubtitles-hash", "opensubtitles-text"):
            cand = _sources_path(video, tag)
            if cand.exists():
                try:
                    cand.unlink()
                except OSError as e:
                    print(f"[upgrade-english] unlink {cand.name!r}: {e}", flush=True)
        # Drop the cached verified.srt — it was derived from candidates
        # we just nuked (or from whisper-fallback), so re-running the
        # verify+resync stage is required for any OS hit to land.
        verified = _sources_path(video, "verified")
        if verified.exists():
            try:
                verified.unlink()
            except OSError as e:
                print(f"[upgrade-english] unlink {verified.name!r}: {e}", flush=True)
        # Drop the canonical SRT so the scan loop re-queues the pipeline.
        srt = video.with_suffix(".srt")
        if srt.exists():
            try:
                srt.unlink()
                cleared += 1
            except OSError as e:
                print(f"[upgrade-english] unlink {srt.name!r}: {e}", flush=True)
    return {"ok": True, "cleared": cleared}


@app.post("/api/bt/translate-zh", status_code=200)
async def bt_translate_zh(req: BtTranslateZhRequest):
    """Submit every video in `wrapper` to the Chinese-translation worker.
    Each video uses per-cue Gemini Flash Lite (with sliding-window
    context) to translate the sibling `<stem>.srt` into
    `<stem>.zh-tw.srt`, or stamps `<stem>.zh-tw.srt.error` on failure.

    `wrapper` is the bt-side wrapper name — videos resolve via the
    canonical-path manifest written by bt_filter, so this works
    regardless of how Movies/TV split out under /artifact.

    Refuses if:
      - the torrent is still downloading (any `.aria2` on bt side)
      - no manifest exists for this wrapper (bt_filter hasn't run)
      - any video lacks the `※ annotated` sentinel (without an
        annotated English SRT, the translator has nothing to start from)

    Idempotent — videos that already have `<stem>.zh-tw.srt` get skipped
    inside the worker. Calling again after a partial failure clears
    `.error` stamps, so the button doubles as retry."""
    if any((BT_ROOT / req.wrapper).rglob("*.aria2")):
        raise HTTPException(status_code=409, detail="torrent still downloading")
    canonical_videos = load_manifest(req.wrapper)
    if not canonical_videos:
        raise HTTPException(
            status_code=404,
            detail=f"no artifact manifest for wrapper: {req.wrapper}",
        )

    videos: list[Path] = []
    for video in canonical_videos:
        if not video.is_file():
            continue
        srt = video.with_suffix(".srt")
        # Canonical SRT is only written atomically as the pipeline's
        # final step (annotation included), so existence here implies
        # the English transcript is finished and ready for translation.
        if not srt.exists():
            raise HTTPException(status_code=409, detail=f"{video.name} has no SRT yet")
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


class BtTranslateZhFileRequest(BaseModel):
    path: str


@app.post("/api/bt/translate-zh-file", status_code=200)
async def bt_translate_zh_file(req: BtTranslateZhFileRequest):
    """Submit a single video for Chinese translation. Same worker pool
    and idempotency rules as the per-torrent endpoint, just scoped to
    one file — for when only an episode or two need a translation pass
    rather than the whole season."""
    path = _validate_bt_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="video not found")
    if path.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="not a video file")
    srt = path.with_suffix(".srt")
    # Canonical SRT is only written atomically as the pipeline's final
    # step (annotation included); existence implies "ready to translate".
    if not srt.exists():
        raise HTTPException(status_code=409, detail="no SRT yet — wait for pipeline to finish")

    zh_path = path.parent / f"{path.stem}{ZH_SUFFIX}"
    if zh_path.exists():
        return {"ok": True, "queued": 0, "reason": "already translated"}

    with _translating_lock:
        in_flight = str(path) in _translating and not _translating[str(path)].done()
    if in_flight:
        return {"ok": True, "queued": 0, "reason": "already in flight"}

    err_path = Path(str(zh_path) + ".error")
    if err_path.exists():
        try:
            err_path.unlink()
        except OSError:
            pass

    _submit_translate(path)
    return {"ok": True, "queued": 1}


class BtRetryRequest(BaseModel):
    path: str


@app.post("/api/bt/retry", status_code=200)
async def bt_retry(req: BtRetryRequest):
    """Clear failure state for a video so the scan loop replays its pipeline.

    Deletes: the canonical SRT + every failure sidecar
    (`.whisper-failed`, `.whisper-polluted`, `.annotate-failed`).
    Keeps the `_sources/` candidate cache — whisper output and OS hits
    stick around so the next pipeline run replays cheaply (no GPU re-pass,
    no OS quota re-burn). For a hard reset that wipes cached sources too,
    manual rm under /artifact/_sources/ is the right escape hatch.
    """
    path = _validate_bt_path(req.path)
    for target in (
        path.with_suffix(".srt"),
        whisper_failed_path(path),
        whisper_polluted_path(path),
        annotate_failed_path(path),
    ):
        if target.exists():
            try:
                target.unlink()
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"unlink {target.name} failed: {e}")
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

def _run_pending_filter():
    """For every aria2 wrapper under /bt that has finished downloading
    (no `.aria2` control files) and hasn't been filtered into /artifact
    yet, run the LLM cleanup + SRT-match + hardlink pass.

    Sentinel lives at /artifact/_processed/<wrapper>.filtered (not in
    the bt-side wrapper, which is read-only to us, and not in the
    canonical artifact output dirs, because those derive from LLM-decided
    titles not the bt wrapper name).

    Idempotent at the bt_filter.filter_wrapper level (sentinel skip),
    so an interrupted-restart still gets one clean attempt.
    """
    if not BT_ROOT.exists():
        return
    for wrapper in BT_ROOT.iterdir():
        if not wrapper.is_dir():
            continue
        # In-flight aria2 — wait until every piece has verified.
        if any(wrapper.rglob("*.aria2")):
            continue
        if bt_filter_sentinel_for(wrapper.name).exists():
            continue
        try:
            filter_wrapper(wrapper)
        except Exception:
            traceback.print_exc()


def _queue_pending_bt_work():
    """Decide what each bt video needs and enqueue the right worker.

    Three states by file existence alone — no marker reads:

      - canonical SRT exists      → SKIP (pipeline finished; canonical is
                                    only written atomically after annotation)
      - whisper-failed sidecar    → SKIP (user must ↻ to retry)
      - whisper-polluted sidecar  → SKIP (whisper hallucinated; user must
                                    drop a candidate or refetch OS then ↻)
      - annotate-failed sidecar   → SKIP (user must ↻ to retry)
      - none of the above         → queue process_bt_file (which itself
                                    resumes at whichever pipeline stage
                                    is missing in `_sources/`)

    Manual SRT drops at the canonical path are accepted as final — pipeline
    won't touch them. Drop into `_sources/<stem>.bundled.srt` instead if
    you want the content gate to evaluate your candidate before promotion.

    Global BT_PIPELINE_ENABLED gate: when set to 0, both this loop and
    the filter loop short-circuit — the container becomes a pure aria2
    downloader. See BT_PIPELINE_ENABLED docstring for the rationale.
    """
    if not BT_PIPELINE_ENABLED:
        return
    _run_pending_filter()
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
        if (item["has_srt"] or item["whisper_error"]
                or item["whisper_polluted_error"] or item["annotate_error"]):
            continue
        job_id = str(uuid.uuid4())
        job = _new_bt_job(job_id, item["path"])
        upsert_job(job)
        executor.submit(process_bt_file, job_id)


async def _bt_work_loop():
    """Periodically scan /bt for files needing whisper or annotation, and queue them."""
    if not BT_PIPELINE_ENABLED:
        print("[bt-work-loop] BT_PIPELINE_ENABLED=0 — filter + pipeline halted; "
              "downloads still active. Set to 1 and restart to resume.", flush=True)
    while True:
        try:
            await asyncio.to_thread(_queue_pending_bt_work)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(BT_SCAN_INTERVAL)


