import asyncio
import os
import shutil
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

from bt_filter import (
    ARTIFACT_ROOT,
    BT_ROOT,
    _sentinel_for as bt_filter_sentinel_for,
    _sources_path,
    filter_wrapper,
    load_manifest,
)
from archive import archive_paths_for
from gpu_lock import release_all_held
from srt_source import (
    all_failure_sidecar_paths,
    any_failure_reason,
)
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from tasks import enumerate_playlist, executor, process_bt_file, process_video

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")
# aria2 subprocess management lives in a separate container so BT traffic
# routes through Surfshark gluetun VPN while all other transcribe API
# calls stay on my_network direct. See utilities/aria2/ for the service.
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

# Master switch for the automatic reconciler that runs bt_filter (LLM
# classification + hardlink to /artifact) and dispatches the downstream
# whisper / annotate / translate pipeline. When 0, both reconcile passes
# early-return and the container becomes a pure aria2 harness: torrents
# download and seed as usual, but nothing else happens. Manual endpoints
# (`/api/bt/transcribe`, `/api/bt/retry`) still work — user-triggered
# actions deliberately bypass the switch since they express explicit
# intent. Default 1 (normal behavior). Set 0 for a "download-only" period.
BT_PIPELINE_ENABLED = os.environ.get("BT_PIPELINE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off", "")

# State model: file existence alone — no marker reads, no jobs.json
# overlay. Canonical `.srt` + `.zh-tw.srt` both exist = pipeline
# finished all stages (whisper / verify / annotate / translate);
# `.pipeline-failed` sidecar = pipeline halted at some stage with
# reason recorded. The ↻ button deletes canonical + zh + failure
# sidecar to let the reconciler re-dispatch the pipeline; cached
# `_sources/` candidates + canonical .srt (if already written) are
# kept so replay only re-does the stages that were missing.

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

    reconcile_task = asyncio.create_task(_bt_reconcile_loop())

    try:
        yield
    finally:
        reconcile_task.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
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
    """Walk BT_ROOTS, return one entry per video file. Filesystem only.

    Each item carries a `wrapper` field naming the bt-side torrent
    wrapper it was hardlinked from (by inode match); the frontend uses
    it to group /artifact items under their originating torrent card
    for the per-torrent `→ E` upgrade-English action. Empty string when
    the bt-side original has been removed (orphaned canonical) or was
    manually dropped without going through bt_filter."""
    out = []
    now = time.time()
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
            pipeline_error = any_failure_reason(video)
            # Chinese sub is now the pipeline's final stage — presence
            # of .zh-tw.srt means fully done, absence means either the
            # pipeline is still running (has_srt=False, no failure) or
            # translation failed (has_srt=True + pipeline_error set with
            # kind='translate failed'). No separate zh error track.
            zh_path = video.parent / f"{video.stem}{ZH_SUFFIX}"
            has_zh_srt = zh_path.exists()
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
                "pipeline_error": pipeline_error,
                "has_zh_srt": has_zh_srt,
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


# Append-only log of every magnet the user has submitted through the
# bt tab. Useful for re-submitting after a delete_torrent, or for
# reconstructing what was in the library after a data wipe. Lives on
# the bind mount so it survives container restarts.
MAGNET_HISTORY_FILE = Path("/app/data/magnet-history.tsv")


def _record_magnet(magnet: str, wrapper: str) -> None:
    """Tab-separated append: ISO timestamp \\t wrapper name \\t magnet URI.
    O_APPEND write is atomic for line-sized payloads on POSIX (< PIPE_BUF
    = 4 KB), so concurrent submissions can't interleave. Failure to write
    logs but doesn't fail the request — the magnet is already submitted to
    aria2, and the history file is auxiliary."""
    line = f"{_now()}\t{wrapper}\t{magnet}\n"
    try:
        MAGNET_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with MAGNET_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        print(f"[magnet-history] append failed: {exc}", flush=True)


@app.post("/api/bt/magnet", status_code=201)
async def submit_magnet(req: BtMagnetRequest):
    """Proxy to the aria2 sidecar's POST /torrents so BT traffic exits
    through gluetun's VPN tunnel. Response body is passed through
    verbatim (currently `{"wrapper": "..."}`). Also appends a record
    of the submission to MAGNET_HISTORY_FILE."""
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        r = _aria2_client.post("/torrents", json={"magnet": req.magnet})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"aria2 sidecar unreachable: {exc}")
    if r.status_code >= 400:
        detail = _extract_detail(r) or f"aria2 sidecar returned {r.status_code}"
        raise HTTPException(status_code=r.status_code, detail=detail)
    body = r.json()
    _record_magnet(req.magnet, body.get("wrapper", ""))
    return body


@app.get("/api/bt/torrents")
async def list_torrents():
    """Bt-tab poll endpoint. Returns `{aria2_up, torrents}`:
      - aria2_up: bool — is the sidecar reachable right now? Frontend
        uses this to disable the magnet input + submit button when
        false, since new torrent submission has no local fallback
        (aria2c is only installed in the sidecar).
      - torrents: [{name, phase, progress?}, ...] — live from aria2 if
        it's up, else a filesystem-only fallback listing off the
        shared /bt bind-mount with every entry marked
        `phase=orphaned`. Wrapper names are truthfully known locally;
        only live phase / progress lives in the sidecar's in-memory
        subprocess registry, and `orphaned` is the existing phase for
        "wrapper exists but no subprocess", so the frontend renders
        this uniformly with a genuinely orphaned wrapper."""
    try:
        r = _aria2_client.get("/torrents")
        r.raise_for_status()
        return {"aria2_up": True, "torrents": r.json()}
    except httpx.HTTPError as exc:
        print(f"[list_torrents] aria2 sidecar unreachable ({exc}); "
              f"falling back to local /bt listing", flush=True)
        return {"aria2_up": False, "torrents": _fallback_list_torrents()}


def _fallback_list_torrents() -> list[dict]:
    """Local /bt walk used when the aria2 sidecar is unreachable.
    Wrappers get phase=orphaned uniformly — we can't tell downloading
    from seeding without aria2's Popen dict, and both are meaningless
    without a live subprocess anyway."""
    if not BT_ROOT.exists():
        return []
    return [
        {"name": w.name, "phase": "orphaned"}
        for w in sorted(BT_ROOT.iterdir())
        if w.is_dir()
    ]


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

    # Kill any live aria2 subprocess via the sidecar — the sidecar owns
    # the Popen handles, we can't SIGTERM aria2c from here even though
    # the wrapper dir is shared. Sidecar failure isn't fatal; if aria2
    # is down its subprocesses died with it, so nothing needs killing.
    try:
        r = _aria2_client.delete(f"/torrents/{wrapper}")
        if r.status_code >= 400 and r.status_code != 404:
            print(f"[delete_torrent] aria2 sidecar returned {r.status_code} "
                  f"for {wrapper!r}: {r.text[:200]}", flush=True)
    except httpx.HTTPError as exc:
        print(f"[delete_torrent] aria2 sidecar unreachable while deleting "
              f"{wrapper!r}: {exc}", flush=True)

    # rmtree /bt/<wrapper>/ from here. /bt is a shared bind-mount so
    # either container can do it; doing it here means aria2 outages
    # don't leave orphaned wrappers on disk. Idempotent: if the aria2
    # sidecar already rmtreed as part of its DELETE handler, this is a
    # no-op. Safety-check the resolved path so nothing outside BT_ROOT
    # can be touched even under adversarial input.
    bt_wrapper_dir = BT_ROOT / wrapper
    try:
        resolved = bt_wrapper_dir.resolve()
        resolved.relative_to(BT_ROOT.resolve())
    except (OSError, ValueError):
        resolved = None
    if resolved is not None and resolved.exists() and resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)

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


class BtRetryRequest(BaseModel):
    path: str


@app.post("/api/bt/retry", status_code=200)
async def bt_retry(req: BtRetryRequest):
    """Deep-reset a video so the reconciler replays the whole pipeline
    from scratch.

    Nukes:
      - canonical SRT + Chinese SRT
      - `.pipeline-failed` sidecar (and legacy variants: `.whisper-failed`,
        `.whisper-polluted`, `.annotate-failed`, `.pipeline-crashed`)
      - every `_sources/<stem>.*.srt` cache entry (whisper output, OS
        candidates, verified, embedded, pgs-ocr, bundled) — forces a
        fresh whisper pass + fresh OS quota shots on replay
      - the corresponding archive entries (`data/archive/<title>/…/<stem>.srt`
        + `.zh-tw.srt`) — otherwise Stage 0's archive attach would just
        pull the same wrong SRT back on the next pipeline run, defeating
        the whole point of retrying

    Rationale: most `.pipeline-failed` cases the user encounters (whisper
    polluted, no viable OS candidate) aren't fixable by cheap re-annotate
    — the failure is baked into cached `_sources/` outputs and the
    already-mirrored archive. A shallow retry that keeps either would
    just re-observe the same failure. Deep reset is the honest recovery
    path; the ~30 min GPU cost is acceptable since retries are rare.
    """
    path = _validate_bt_path(req.path)
    zh_path = path.parent / f"{path.stem}{ZH_SUFFIX}"
    targets = [path.with_suffix(".srt"), zh_path, *all_failure_sidecar_paths(path)]
    # Sweep every _sources/<stem>.*.srt for this video.
    sources_dir = _sources_path(path, "x").parent   # tag arbitrary; dir same
    if sources_dir.is_dir():
        stem_prefix = path.stem + "."
        for f in sources_dir.iterdir():
            if f.is_file() and f.name.startswith(stem_prefix) and f.suffix == ".srt":
                targets.append(f)
    # Nuke the archive mirror for this video so Stage 0 can't hand back
    # the same wrong SRT on replay.
    archived = archive_paths_for(path)
    if archived is not None:
        targets.extend(archived)
    for target in targets:
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

def _reconcile_bt_filter():
    """Reconcile filter state: every finished wrapper should have a
    filter sentinel. Enqueue filter_wrapper for wrappers that don't.

    Desired state per wrapper:
      - no `.aria2` control files (aria2 finished)
      - sentinel exists at /artifact/_processed/<wrapper>.filtered

    Sentinel lives at /artifact/_processed/<wrapper>.filtered (not in
    the bt-side wrapper, which is read-only to us, and not in the
    canonical artifact output dirs, because those derive from LLM-decided
    titles not the bt wrapper name).

    Idempotent at the bt_filter.filter_wrapper level (sentinel skip),
    so an interrupted-restart still gets one clean attempt.
    """
    if not BT_ROOT.exists():
        return
    now = time.time()
    for wrapper in BT_ROOT.iterdir():
        if not wrapper.is_dir():
            continue
        # In-flight aria2 — wait until every piece has verified.
        if any(wrapper.rglob("*.aria2")):
            continue
        if bt_filter_sentinel_for(wrapper.name).exists():
            continue
        # Race: aria2 creates the wrapper dir + saves .torrent as soon as
        # it receives the magnet, but takes seconds-to-tens-of-seconds
        # to fetch metadata + allocate the actual video files. During
        # that window the wrapper has no video files and no `.aria2`
        # yet — filter_wrapper would `_collect_videos()==[]` and write
        # an empty sentinel that permanently freezes the wrapper out.
        # Guard with the newest child-file mtime: aria2 bumps at least
        # one file's mtime whenever it allocates a new file or writes
        # a piece, so this stays "recent" for the full download and
        # only clears well after aria2 is done.
        try:
            newest = max(
                (p.stat().st_mtime for p in wrapper.rglob("*") if p.is_file()),
                default=None,
            )
        except OSError:
            continue
        if newest is None or now - newest < MTIME_GRACE_SECONDS:
            continue
        try:
            filter_wrapper(wrapper)
        except Exception as exc:
            traceback.print_exc()
            # filter_wrapper's own known-failure paths all write an empty
            # sentinel + fire notify_filter_failure before returning.
            # An unexpected exception here (bug, OOM, disk full) escaped
            # those, leaving no sentinel — without one, the next scan
            # tick would re-run filter_wrapper, hit the same exception,
            # notify again, forever. Symmetric to _catch_unhandled in
            # tasks.py: mark + notify.
            from bt_filter import _write_sentinel
            from notifier import notify_filter_failure
            try:
                _write_sentinel(wrapper.name, [])
            except Exception:
                pass
            try:
                notify_filter_failure(wrapper.name, f"unexpected exception: {exc}")
            except Exception:
                pass


def _reconcile_bt_state():
    """Reconcile bt state — top-level of the reconciler loop. Runs
    _reconcile_bt_filter first (so newly-filtered wrappers immediately
    contribute canonical videos to the pipeline stage in the same tick),
    then _reconcile_bt_pipeline for each canonical video.

    Desired state per canonical video (decided by file existence alone,
    no marker reads):

      - canonical SRT exists    → satisfied; SKIP
      - `.pipeline-failed`      → explicit failure recorded; SKIP
                                  (user must ↻ to clear sidecar and opt back in)
      - in-flight job present   → worker running; SKIP
      - none of the above       → enqueue process_bt_file (which itself
                                  resumes at whichever pipeline stage is
                                  missing in `_sources/` — brand-new video
                                  starts from stage 0, crash-recovered
                                  video skips already-cached stages)

    Manual SRT drops at the canonical path are accepted as final — pipeline
    won't touch them. Drop into `_sources/<stem>.bundled.srt` instead if
    you want the content gate to evaluate your candidate before promotion;
    the next reconcile tick sees "no canonical, no sidecar" and enqueues
    process_bt_file, which reads _sources/ during stage selection.

    Global BT_PIPELINE_ENABLED gate: when set to 0, both reconcile passes
    short-circuit — the container becomes a pure aria2 downloader. See
    BT_PIPELINE_ENABLED docstring for the rationale.
    """
    if not BT_PIPELINE_ENABLED:
        return
    _reconcile_bt_filter()
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
        if item["pipeline_error"]:
            continue
        # "Done" = both English + Chinese SRT exist. If only English is
        # present (crash recovery between annotate and translate, or user
        # cleared .pipeline-failed after a translate-stage failure),
        # process_bt_file resumes at the translate stage — canonical .srt
        # is cached, whisper doesn't re-run.
        if item["has_srt"] and item["has_zh_srt"]:
            continue
        job_id = str(uuid.uuid4())
        job = _new_bt_job(job_id, item["path"])
        upsert_job(job)
        executor.submit(process_bt_file, job_id)


async def _bt_reconcile_loop():
    """Reconciler loop for the bt pipeline. Runs every BT_SCAN_INTERVAL.

    Model: this is a declarative reconciler, NOT a retry loop. Each tick
    reads current filesystem state, compares against desired state, and
    enqueues work to close the gap. Idempotent by construction — items
    already at desired state (or with an explicit failure sidecar, or an
    in-flight job) are skipped.

    Desired state:
      - Every finished bt wrapper (no `.aria2` files) has a filter sentinel
        under /artifact/_processed/, meaning bt_filter has classified and
        hardlinked its videos into /artifact/Movies|TV/.
      - Every canonical video under /artifact/ has either an annotated
        `.srt` sibling, OR a `.pipeline-failed` sidecar explaining why not.

    Failure model — the tick does NOT retry failed work automatically:
      - `.pipeline-failed` sidecar blocks re-dispatch; UI ↻ button clears
        the sidecar to opt back in.
      - Successful items (canonical `.srt` exists) are never re-dispatched.
      - Only "no result, no explicit failure, not in flight" gets enqueued —
        i.e. brand-new videos, or ones whose worker died before it could
        stamp a sidecar (container OOM / restart). The latter resumes from
        cached `_sources/` stage output rather than redoing everything.

    So in steady state each tick is a no-op — the primary work is
    dispatching brand-new aria2 downloads. Crash recovery is a free
    side-effect of the same code path.
    """
    if not BT_PIPELINE_ENABLED:
        print("[bt-reconcile] BT_PIPELINE_ENABLED=0 — filter + pipeline halted; "
              "downloads still active. Set to 1 and restart to resume.", flush=True)
    while True:
        try:
            await asyncio.to_thread(_reconcile_bt_state)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(BT_SCAN_INTERVAL)


