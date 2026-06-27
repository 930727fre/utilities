"""FastAPI app for live-hls. Endpoints map 1:1 with PLAN.md:

    POST   /api/start              { path } → { session_id, master_url, duration_seconds }
    GET    /api/{sid}/master.m3u8                                → playlist text
    GET    /api/{sid}/seg_{N}.ts                                 → segment bytes
    GET    /api/{sid}/status                                     → diagnostics
    DELETE /api/{sid}                                            → end + cleanup

All segment I/O happens on a thread (`asyncio.to_thread`) — the wait loop
inside `transcoder.serve_segment` blocks on filesystem polling and would
otherwise pin the event loop.
"""
import asyncio
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import transcoder

_sessions: dict[str, transcoder.HlsSession] = {}
_sessions_lock = threading.Lock()
_GC_INTERVAL = 30.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    transcoder.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    gc_task = asyncio.create_task(_gc_loop())
    try:
        yield
    finally:
        gc_task.cancel()
        # Tear down every live session so ffmpeg subprocesses don't outlive us.
        with _sessions_lock:
            to_destroy = list(_sessions.values())
            _sessions.clear()
        for s in to_destroy:
            try:
                transcoder.destroy_session(s)
            except Exception:
                traceback.print_exc()


async def _gc_loop():
    while True:
        try:
            await asyncio.sleep(_GC_INTERVAL)
            await asyncio.to_thread(_gc_pass)
        except asyncio.CancelledError:
            return
        except Exception:
            traceback.print_exc()


def _gc_pass():
    with _sessions_lock:
        idle = [(sid, s) for sid, s in _sessions.items() if s.is_idle()]
    for sid, s in idle:
        with _sessions_lock:
            _sessions.pop(sid, None)
        try:
            transcoder.destroy_session(s)
            print(f"[gc] reaped idle session {sid}", flush=True)
        except Exception:
            traceback.print_exc()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Endpoints ─────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    path: str


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/start")
async def start(req: StartRequest):
    try:
        path = transcoder.validate_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {req.path}")

    try:
        session = await asyncio.to_thread(transcoder.create_session, path)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    with _sessions_lock:
        _sessions[session.sid] = session

    print(f"[start] sid={session.sid} path={path} duration={session.duration_seconds:.1f}s",
          flush=True)

    return {
        "session_id": session.sid,
        "master_url": f"/api/{session.sid}/master.m3u8",
        "duration_seconds": session.duration_seconds,
    }


def _get_session(sid: str) -> transcoder.HlsSession:
    with _sessions_lock:
        s = _sessions.get(sid)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@app.get("/api/{sid}/master.m3u8")
async def get_master(sid: str):
    s = _get_session(sid)
    s.touch()
    master_path = s.work_dir / "master.m3u8"
    if not master_path.exists():
        raise HTTPException(status_code=410, detail="session work dir missing master playlist")
    return FileResponse(master_path, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/{sid}/seg_{seg_n}.ts")
async def get_segment(sid: str, seg_n: int):
    s = _get_session(sid)
    seg_path = await asyncio.to_thread(transcoder.serve_segment, s, seg_n)
    if seg_path is None:
        raise HTTPException(status_code=504, detail=f"timed out waiting for seg_{seg_n}")
    return FileResponse(seg_path, media_type="video/mp2t",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/{sid}/status")
async def status(sid: str):
    s = _get_session(sid)
    proc = s.proc
    return {
        "sid": s.sid,
        "source": str(s.source_path),
        "duration_seconds": s.duration_seconds,
        "segment_length": s.segment_length,
        "proc_alive": proc is not None and proc.poll() is None,
        "proc_pid": proc.pid if (proc is not None and proc.poll() is None) else None,
        "proc_start_seg": s.proc_start_seg,
        "current_seg": transcoder.current_ffmpeg_index(s),
        "idle_seconds": round(time.time() - s.last_request_at, 1),
    }


@app.delete("/api/{sid}")
async def delete_session(sid: str):
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if s is not None:
        await asyncio.to_thread(transcoder.destroy_session, s)
        print(f"[delete] sid={sid}", flush=True)
    return {"ok": True}
