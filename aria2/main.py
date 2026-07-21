"""Thin REST wrapper around bt_torrents (which now talks to a persistent
aria2c daemon via JSON-RPC).

Endpoints
    POST   /torrents          {"magnet": "magnet:?..."}         → {"wrapper": "..."}
    POST   /probe             {"magnet": "magnet:?..."}         → {"size_bytes": N, "name": "..."}
    GET    /torrents                                             → [{name, phase, progress?}, ...]
    DELETE /torrents/{wrapper}                                   → 204
    GET    /health                                               → {"ok": true}

Lifespan (order matters):
    startup  → start_daemon()   (spawn aria2c, wait for RPC)
               resume_all()     (scan wrappers, re-add any .torrent
                                 whose infohash isn't already in
                                 aria2's queue)
    shutdown → stop_daemon()    (RPC aria2.shutdown → SIGTERM fallback)

All aria2c outbound traffic exits through gluetun's VPN tunnel since
this container uses network_mode: "service:aria2-gluetun". If gluetun
dies or the tunnel drops, both the daemon and this sidecar lose
connectivity — the implicit kill-switch prevents clear-text leaks
on VPN failure.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import bt_torrents


@asynccontextmanager
async def lifespan(app: FastAPI):
    bt_torrents.start_daemon()
    bt_torrents.resume_all()
    try:
        yield
    finally:
        bt_torrents.stop_daemon()


app = FastAPI(lifespan=lifespan)


class MagnetRequest(BaseModel):
    magnet: str


@app.get("/health")
async def health():
    """Cheap probe. Just confirms the FastAPI process is up + reachable
    on this netns (which implies gluetun's tunnel is up too, since we
    share its network namespace)."""
    return {"ok": True}


@app.post("/torrents", status_code=201)
async def submit(req: MagnetRequest):
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        wrapper = bt_torrents.submit(req.magnet)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"aria2 addUri failed: {exc}")
    return {"wrapper": wrapper}


@app.post("/probe")
async def probe(req: MagnetRequest):
    """Fetch a magnet's .torrent metadata (size + name) without
    downloading the payload. Used by transcribe for a disk-headroom
    preflight before committing to the full download."""
    if not req.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="must be a magnet: URI")
    try:
        return bt_torrents.probe(req.magnet)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"aria2 probe failed: {exc}")


@app.get("/torrents")
async def list_torrents():
    return bt_torrents.list_torrents()


@app.get("/stats")
async def stats():
    """Global bandwidth + cumulative transfer for the BT-tab header."""
    try:
        return bt_torrents.global_stats()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"aria2 stats failed: {exc}")


@app.delete("/torrents/{wrapper}", status_code=204)
async def delete_torrent(wrapper: str):
    bt_torrents.delete(wrapper)
    return None
