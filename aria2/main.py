"""Thin REST wrapper around bt_torrents.

Endpoints
    POST   /torrents          {"magnet": "magnet:?..."}         → {"wrapper": "..."}
    GET    /torrents                                             → [{name, phase, progress?}, ...]
    DELETE /torrents/{wrapper}                                   → 204
    GET    /health                                               → {"ok": true, "forwarded_port": <int|null>}

Lifespan handles subprocess bookkeeping across container restarts:
    startup  → bt_torrents.resume_all()  (re-attach aria2c to any
               half-finished download that survived the restart)
    shutdown → bt_torrents.shutdown()    (kill live subprocesses cleanly)

All aria2c outbound traffic exits through gluetun's VPN tunnel since
this container uses network_mode: "service:aria2-gluetun". If gluetun
dies or the tunnel drops, this container loses connectivity — the
implicit kill-switch prevents clear-text leaks on VPN failure.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import bt_torrents


@asynccontextmanager
async def lifespan(app: FastAPI):
    bt_torrents.resume_all()
    try:
        yield
    finally:
        bt_torrents.shutdown()


app = FastAPI(lifespan=lifespan)


class MagnetRequest(BaseModel):
    magnet: str


@app.get("/health")
async def health():
    """Cheap probe. Reports the PIA forwarded port so upstream can tell
    whether seeding is currently reachable (null before tunnel handshake
    or if the forwarded-port file isn't accessible)."""
    return {
        "ok": True,
        "forwarded_port": bt_torrents._read_forwarded_port(),
    }


@app.post("/torrents", status_code=201)
async def submit(req: MagnetRequest):
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        wrapper = bt_torrents.submit(req.magnet)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"aria2c launch failed: {exc}")
    return {"wrapper": wrapper}


@app.get("/torrents")
async def list_torrents():
    return bt_torrents.list_torrents()


@app.delete("/torrents/{wrapper}", status_code=204)
async def delete_torrent(wrapper: str):
    bt_torrents.delete(wrapper)
    return None
