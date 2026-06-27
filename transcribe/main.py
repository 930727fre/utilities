import asyncio
import json
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import bt_torrents
import hls_precompute
from annotate import annotate_executor, annotate_job
from bt_filter import pair_wrapper
from gpu_lock import release_all_held
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from subs_finder import find_subs
from translator import translate_video_zh, translator_executor
from tasks import enumerate_playlist, executor, process_bt_file, process_video

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")

DOWNLOADS_DIR = Path("/app/data/downloads")
DERIVED_ROOT = Path("/app/data/derived")
BT_ROOT = Path("/bt")
PROGRESS_FILE = Path("/app/data/progress.json")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}
MTIME_GRACE_SECONDS = 60
BT_SCAN_INTERVAL = 30

# In-flight translate-to-zh futures keyed by absolute video path.
_translating: dict[str, Future] = {}
_translating_lock = threading.Lock()

# Progress JSON in-process lock (file IO is atomic temp+rename).
_progress_lock = threading.Lock()


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


def _derived_dir_for(video: Path) -> Path:
    """video at /bt/<wrapper>/...; return /app/data/derived/<wrapper>/<stem>/."""
    wrapper = video.relative_to(BT_ROOT).parts[0]
    return DERIVED_ROOT / wrapper / video.stem


def _hls_complete(derived_dir: Path) -> bool:
    return hls_precompute.is_complete(derived_dir)


def _hls_in_flight(derived_dir: Path) -> bool:
    return hls_precompute.is_in_flight(derived_dir)


# ── Progress storage ──────────────────────────────────────────────────────

def _read_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {}
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_progress(d: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, PROGRESS_FILE)


def _resume_at_seconds(wrapper: str, stem: str) -> float:
    key = f"{wrapper}/{stem}"
    with _progress_lock:
        return float(_read_progress().get(key, {}).get("position_seconds", 0))


def _store_progress(wrapper: str, stem: str, position_seconds: float) -> None:
    key = f"{wrapper}/{stem}"
    with _progress_lock:
        d = _read_progress()
        d[key] = {
            "position_seconds": position_seconds,
            "updated_at": _now(),
        }
        _write_progress(d)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Hard-fail fast if the shared whisper service isn't reachable.
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
    DERIVED_ROOT.mkdir(parents=True, exist_ok=True)

    # Reset orphaned mid-flight jobs from a prior crash.
    jobs = read_jobs()
    changed = False
    for job in jobs:
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
            job["status"] = "SUCCESS"
            job["annotation_error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned annotation {job['job_id']} -> SUCCESS", flush=True)
    if changed:
        write_jobs(jobs)

    # Re-spawn aria2c for any wrapper with a half-finished download.
    bt_torrents.resume_all()

    bt_loop_task = asyncio.create_task(_bt_work_loop())

    try:
        yield
    finally:
        bt_loop_task.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        annotate_executor.shutdown(wait=False, cancel_futures=True)
        translator_executor.shutdown(wait=False, cancel_futures=True)
        hls_precompute.hls_executor.shutdown(wait=False, cancel_futures=True)
        bt_torrents.shutdown()
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
        base = job.get("basename")
        if base:
            for ext in ("mp4", "srt"):
                (DOWNLOADS_DIR / f"{base}.{ext}").unlink(missing_ok=True)
        (DOWNLOADS_DIR / f"{job_id}.mp4").unlink(missing_ok=True)
        (DOWNLOADS_DIR / f"{job_id}.srt").unlink(missing_ok=True)

    job["status"] = "DELETED"
    job["updated_at"] = _now()
    upsert_job(job)
    return {"ok": True}


# ── bt API ─────────────────────────────────────────────────────────────────

class BtTranscribeRequest(BaseModel):
    path: str


def _read_error(p: Path) -> str | None:
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip() or "(empty error)"
    except OSError:
        return "(unreadable .error file)"


def _scan_bt() -> list[dict]:
    """Walk /bt, return one entry per video file. State per entry comes
    from derived/<wrapper>/<stem>/."""
    out = []
    now = time.time()
    with _translating_lock:
        in_flight_translate = {p for p, f in _translating.items() if not f.done()}

    if not BT_ROOT.exists():
        return out

    # Wrappers still being written by aria2 are skipped wholesale.
    wrappers_in_flight = {
        w.resolve()
        for w in BT_ROOT.iterdir()
        if w.is_dir() and any(w.rglob("*.aria2"))
    }

    for video in BT_ROOT.rglob("*"):
        if not video.is_file():
            continue
        if video.suffix.lower() not in VIDEO_EXTS:
            continue
        if video.name.startswith("."):
            continue
        try:
            wrapper = BT_ROOT / video.relative_to(BT_ROOT).parts[0]
            if wrapper.resolve() in wrappers_in_flight:
                continue
            wrapper_name = wrapper.name
        except (ValueError, IndexError):
            continue
        try:
            if now - video.stat().st_mtime < MTIME_GRACE_SECONDS:
                continue
        except OSError:
            continue

        derived = DERIVED_ROOT / wrapper_name / video.stem

        english_srt = derived / "english.srt"
        english_err = derived / "english.srt.error"
        annotated_srt = derived / "annotated.srt"
        annotated_err = derived / "annotated.srt.error"
        zh_srt = derived / "zh.srt"
        zh_err = derived / "zh.srt.error"

        has_srt = english_srt.exists() or annotated_srt.exists()
        has_annotation = annotated_srt.exists()
        whisper_error = _read_error(english_err)
        annotate_error = _read_error(annotated_err)

        has_zh_srt = zh_srt.exists()
        zh_error = _read_error(zh_err) if not has_zh_srt else None
        zh_in_flight = str(video) in in_flight_translate

        hls_ready = _hls_complete(derived)
        hls_in_flight_flag = _hls_in_flight(derived)

        try:
            parent_rel = str(video.parent.relative_to(BT_ROOT))
        except ValueError:
            parent_rel = ""
        if parent_rel == ".":
            parent_rel = ""

        out.append({
            "path": str(video),
            "name": video.name,
            "parent": parent_rel,
            "wrapper": wrapper_name,
            "stem": video.stem,
            "has_srt": has_srt,
            "has_annotation": has_annotation,
            "whisper_error": whisper_error,
            "annotate_error": annotate_error,
            "os_failed": None,
            "has_zh_srt": has_zh_srt,
            "zh_in_flight": zh_in_flight,
            "zh_error": zh_error,
            "hls_ready": hls_ready,
            "hls_in_flight": hls_in_flight_flag,
        })
    out.sort(key=lambda x: (x["parent"], x["name"].lower()))
    return out


def _validate_bt_path(path_str: str) -> Path:
    path = Path(path_str).resolve()
    try:
        path.relative_to(BT_ROOT.resolve())
        return path
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Path not under /bt: {path_str}")


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
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        wrapper = bt_torrents.submit(req.magnet)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"aria2c launch failed: {exc}")
    return {"wrapper": wrapper}


@app.get("/api/bt/torrents")
async def list_torrents():
    return bt_torrents.list_torrents()


@app.delete("/api/bt/torrents/{wrapper}")
async def delete_torrent(wrapper: str):
    """Kill subprocess + rmtree the bt wrapper + drop its derived dir."""
    bt_torrents.delete(wrapper)
    # Also clean the derived products for this wrapper — once bt/ is gone
    # those segments are orphaned cache anyway.
    derived = DERIVED_ROOT / wrapper
    if derived.exists():
        try:
            shutil.rmtree(derived)
        except OSError as e:
            print(f"[delete-torrent] derived rmtree failed for {wrapper!r}: {e}", flush=True)
    return {"ok": True}


class BtTranslateZhRequest(BaseModel):
    wrapper: str


@app.post("/api/bt/translate-zh", status_code=200)
async def bt_translate_zh(req: BtTranslateZhRequest):
    """Translate every annotated video in `wrapper` to 繁體中文.

    Refuses if torrent still downloading or any video lacks annotated.srt.
    Idempotent (skips videos with zh.srt). Clears .error stamps so retry."""
    src = (BT_ROOT / req.wrapper).resolve()
    try:
        src.relative_to(BT_ROOT.resolve())
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
        derived = _derived_dir_for(video)
        annotated = derived / "annotated.srt"
        if not annotated.exists():
            raise HTTPException(status_code=409, detail=f"{video.name} not annotated yet")
        videos.append(video)
    videos.sort(key=lambda p: p.name)

    queued = 0
    with _translating_lock:
        in_flight = {p for p, f in _translating.items() if not f.done()}
    for video in videos:
        derived = _derived_dir_for(video)
        zh_path = derived / "zh.srt"
        if zh_path.exists():
            continue
        if str(video) in in_flight:
            continue
        (derived / "zh.srt.error").unlink(missing_ok=True)
        _submit_translate(video)
        queued += 1
    return {"ok": True, "queued": queued, "total": len(videos)}


class BtTranslateZhFileRequest(BaseModel):
    path: str


@app.post("/api/bt/translate-zh-file", status_code=200)
async def bt_translate_zh_file(req: BtTranslateZhFileRequest):
    path = _validate_bt_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="video not found")
    if path.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="not a video file")
    derived = _derived_dir_for(path)
    annotated = derived / "annotated.srt"
    if not annotated.exists():
        raise HTTPException(status_code=409, detail="not annotated yet")

    zh_path = derived / "zh.srt"
    if zh_path.exists():
        return {"ok": True, "queued": 0, "reason": "already translated"}

    with _translating_lock:
        in_flight = str(path) in _translating and not _translating[str(path)].done()
    if in_flight:
        return {"ok": True, "queued": 0, "reason": "already in flight"}

    (derived / "zh.srt.error").unlink(missing_ok=True)
    _submit_translate(path)
    return {"ok": True, "queued": 1}


class BtRetryRequest(BaseModel):
    path: str


@app.post("/api/bt/retry", status_code=200)
async def bt_retry(req: BtRetryRequest):
    """Wipe the derived/<wrapper>/<stem>/ for this video so the scan loop
    re-runs the whole pipeline. Resilient: missing dir is a no-op."""
    path = _validate_bt_path(req.path)
    derived = _derived_dir_for(path)
    if derived.exists():
        try:
            shutil.rmtree(derived)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"rmtree failed: {e}")
    return {"ok": True}


@app.post("/api/bt/transcribe", status_code=201)
async def transcribe_bt_file(req: BtTranscribeRequest):
    path = _validate_bt_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

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


# ── Playback endpoints ────────────────────────────────────────────────────
#
# Flow per click:
#   1. POST /api/play/resolve with the bt path. We compute wrapper+stem,
#      report HLS readiness + subtitle URLs + resume position.
#   2. <video src> hits /api/play/proxy/{wrapper}/{stem}/master.m3u8 which
#      FileResponse's straight from derived/. hls.js follows along for
#      seg_*.ts under the same prefix.
#   3. POST /api/play/progress every ~1s — writes data/progress.json.

class PlayResolveRequest(BaseModel):
    path: str


@app.post("/api/play/resolve")
async def play_resolve(req: PlayResolveRequest):
    path = _validate_bt_path(req.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    derived = _derived_dir_for(path)
    wrapper = path.relative_to(BT_ROOT).parts[0]
    stem = path.stem
    item_key = f"{wrapper}/{stem}"

    ready = _hls_complete(derived)

    subtitles = []
    en_srt = derived / "annotated.srt"
    if not en_srt.exists():
        en_srt = derived / "english.srt"
    if en_srt.exists():
        subtitles.append({
            "label": "English",
            "srclang": "en",
            "src": f"/api/play/sub?wrapper={wrapper}&stem={stem}&lang=en",
        })
    zh_srt = derived / "zh.srt"
    if zh_srt.exists():
        subtitles.append({
            "label": "繁體中文",
            "srclang": "zh",
            "src": f"/api/play/sub?wrapper={wrapper}&stem={stem}&lang=zh",
        })

    return {
        "item_id": item_key,
        "wrapper": wrapper,
        "stem": stem,
        "name": stem,
        "master_url": f"/api/play/proxy/{wrapper}/{stem}/master.m3u8",
        "subtitles": subtitles,
        "resume_at_seconds": _resume_at_seconds(wrapper, stem),
        "ready": ready,
    }


class PlayProgressEvent(BaseModel):
    item_id: str  # "<wrapper>/<stem>"
    position_seconds: float
    event: str  # "started" | "progress" | "stopped"
    play_session_id: str
    is_paused: bool = False


@app.post("/api/play/progress")
async def play_progress(req: PlayProgressEvent):
    """Persist playback position in data/progress.json keyed by wrapper/stem."""
    if "/" not in req.item_id:
        return {"ok": False, "reason": "bad item_id"}
    wrapper, stem = req.item_id.split("/", 1)
    try:
        _store_progress(wrapper, stem, req.position_seconds)
    except OSError as e:
        print(f"[play_progress] write failed: {e}", flush=True)
        return {"ok": False}
    return {"ok": True}


def _srt_to_vtt(srt_text: str) -> str:
    out = ["WEBVTT", ""]
    for line in srt_text.splitlines():
        if " --> " in line:
            out.append(line.replace(",", "."))
        else:
            out.append(line)
    return "\n".join(out)


@app.get("/api/play/sub")
async def play_sub(wrapper: str, stem: str, lang: str = "en"):
    """Serve derived/<wrapper>/<stem>/<file>.srt as WebVTT for <track>."""
    if "/" in wrapper or "/" in stem:
        raise HTTPException(status_code=400, detail="bad wrapper/stem")
    derived = DERIVED_ROOT / wrapper / stem
    if lang == "en":
        # Prefer annotated.srt over the raw english.srt.
        candidates = [derived / "annotated.srt", derived / "english.srt"]
    elif lang == "zh":
        candidates = [derived / "zh.srt"]
    else:
        raise HTTPException(status_code=400, detail="bad lang")
    p = next((c for c in candidates if c.exists()), None)
    if p is None:
        raise HTTPException(status_code=404, detail="SRT not found")
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")
    return Response(
        content=_srt_to_vtt(raw),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/play/proxy/{wrapper}/{stem}/{filename}")
async def play_proxy(wrapper: str, stem: str, filename: str):
    """Serve derived/<wrapper>/<stem>/<filename> straight from disk.

    Used for both master.m3u8 and seg_*.ts — same prefix, hls.js / Safari
    follows the playlist's relative segment refs back here. FileResponse
    handles Range requests natively (HLS doesn't really need them but
    they're free with Starlette's StaticFiles infrastructure)."""
    if "/" in wrapper or "/" in stem or "/" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="bad path component")
    # Allow only the artifacts ffmpeg writes — never expose .srt etc. via
    # this endpoint (subtitle is /api/play/sub).
    if filename != "master.m3u8" and not (filename.startswith("seg_") and filename.endswith(".ts")):
        raise HTTPException(status_code=400, detail="bad filename")
    fp = DERIVED_ROOT / wrapper / stem / filename
    if not fp.exists():
        raise HTTPException(status_code=404, detail="not found")
    media_type = (
        "application/vnd.apple.mpegurl" if filename.endswith(".m3u8")
        else "video/mp2t"
    )
    return FileResponse(fp, media_type=media_type)


# ── Background bt loop ────────────────────────────────────────────────────

def _ensure_english(video: Path, eng_srt_path: Path | None,
                    derived_dir: Path, in_flight_paths: set[str]) -> None:
    """Make derived/<stem>/english.srt exist (or stamp the error file).

    Order: bundled BT srt (free) → OpenSubtitles (cheap) → whisper (expensive).
    """
    english = derived_dir / "english.srt"
    english_err = derived_dir / "english.srt.error"
    if english.exists() or english_err.exists():
        return
    if str(video) in in_flight_paths:
        return  # whisper job already submitted on a prior tick

    derived_dir.mkdir(parents=True, exist_ok=True)

    # 1. Bundled BT srt — fastest, no API calls.
    if eng_srt_path is not None and eng_srt_path.exists():
        try:
            shutil.copyfile(eng_srt_path, english)
            return
        except OSError as e:
            print(f"[english] bundled copy failed for {video.name}: {e}", flush=True)

    # 2. OpenSubtitles. find_subs caches misses; subsequent ticks are cheap.
    try:
        if find_subs(video, out_path=english) is not None:
            return
    except Exception as e:
        print(f"[english] find_subs raised for {video.name}: {e}", flush=True)

    # 3. Whisper.
    job_id = str(uuid.uuid4())
    job = _new_bt_job(job_id, str(video))
    upsert_job(job)
    executor.submit(process_bt_file, job_id)


def _ensure_annotated(video: Path, derived_dir: Path, in_flight_paths: set[str]) -> None:
    """When english.srt exists but annotated.srt doesn't, queue annotation."""
    english = derived_dir / "english.srt"
    annotated = derived_dir / "annotated.srt"
    if annotated.exists() or (derived_dir / "annotated.srt.error").exists():
        return
    if not english.exists():
        return
    if str(video) in in_flight_paths:
        return  # in-flight whisper or annotate

    job_id = str(uuid.uuid4())
    job = _new_bt_job(job_id, str(video))
    job["status"] = "ANNOTATING"
    upsert_job(job)
    annotate_executor.submit(annotate_job, job_id)


def _scan_and_dispatch():
    """Single scan tick: pair every ready wrapper, ensure derived dirs and
    dispatch missing stages (english / annotated / hls). Translation
    remains user-clicked."""
    if not BT_ROOT.exists():
        return

    jobs = read_jobs()
    in_flight_paths = set()
    for j in jobs:
        if j.get("source") != "bt":
            continue
        if j["status"] in ("PENDING", "DOWNLOADING", "TRANSCRIBING", "ANNOTATING"):
            in_flight_paths.add(j.get("source_path"))

    for wrapper in BT_ROOT.iterdir():
        if not wrapper.is_dir():
            continue
        if any(wrapper.rglob("*.aria2")):
            continue
        try:
            pairings = pair_wrapper(wrapper)
        except Exception:
            traceback.print_exc()
            continue
        for p in pairings:
            derived_dir = DERIVED_ROOT / wrapper.name / p.stem
            try:
                derived_dir.mkdir(parents=True, exist_ok=True)
                _ensure_english(p.video_path, p.eng_srt_path, derived_dir, in_flight_paths)
                _ensure_annotated(p.video_path, derived_dir, in_flight_paths)
                hls_precompute.ensure(p.video_path, derived_dir)
            except Exception:
                traceback.print_exc()


async def _bt_work_loop():
    while True:
        try:
            await asyncio.to_thread(_scan_and_dispatch)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(BT_SCAN_INTERVAL)
