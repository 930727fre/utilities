"""GPU broker — a single-machine queue that serializes GPU-bound work across containers.

Every consumer (xyt, flashcard, keyboard, marker-pipeline) hits POST /acquire before
loading a model and DELETE /lease/{token} after. Acquire blocks until the GPU is free,
so consumers can assume they have exclusive access for the duration of their lease.

State is in-memory (one holder + a FIFO queue + a short history ring). A broker restart
clears it cleanly; consumers fall back to "proceed without lock" if the broker is
unreachable, so a broker outage degrades to "no coordination" rather than "everything
hangs."
"""
import asyncio
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

HISTORY_LEN = 30


class AcquireRequest(BaseModel):
    container: str
    workload: str
    eta_seconds: Optional[float] = None


class Broker:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._holder: Optional[dict] = None
        self._queue: list[dict] = []
        self._history: deque = deque(maxlen=HISTORY_LEN)

    def _set_holder(self, entry: dict):
        now = time.time()
        self._holder = {
            "token": entry["token"],
            "container": entry["container"],
            "workload": entry["workload"],
            "eta_seconds": entry["eta_seconds"],
            "started_at": now,
            "waited_seconds": now - entry["queued_at"],
        }

    async def acquire(self, container: str, workload: str, eta: Optional[float]) -> tuple[str, float]:
        token = uuid.uuid4().hex
        event = asyncio.Event()
        entry = {
            "token": token,
            "container": container,
            "workload": workload,
            "eta_seconds": eta,
            "queued_at": time.time(),
            "event": event,
        }

        async with self._lock:
            if self._holder is None:
                self._set_holder(entry)
                event.set()
            else:
                self._queue.append(entry)
                print(f"[broker] queue: {container}/{workload} (pos={len(self._queue)})", flush=True)

        try:
            await event.wait()
        except asyncio.CancelledError:
            # client disconnected mid-wait — clean up
            async with self._lock:
                if entry in self._queue:
                    self._queue.remove(entry)
                elif self._holder and self._holder["token"] == token:
                    # we were just promoted; release and promote next
                    self._holder = None
                    self._promote_next_unlocked()
            raise

        print(f"[broker] grant: {container}/{workload}", flush=True)
        return token, self._holder["waited_seconds"]

    def _promote_next_unlocked(self):
        """Caller must hold self._lock."""
        if self._queue:
            next_entry = self._queue.pop(0)
            self._set_holder(next_entry)
            next_entry["event"].set()

    async def release(self, token: str) -> bool:
        async with self._lock:
            if not self._holder or self._holder["token"] != token:
                return False
            now = time.time()
            self._history.appendleft({
                "container": self._holder["container"],
                "workload": self._holder["workload"],
                "duration_seconds": now - self._holder["started_at"],
                "waited_seconds": self._holder["waited_seconds"],
                "ended_at": now,
            })
            print(f"[broker] release: {self._holder['container']}/{self._holder['workload']} "
                  f"({now - self._holder['started_at']:.1f}s)", flush=True)
            self._holder = None
            self._promote_next_unlocked()
            return True

    async def force_release(self):
        """Admin: drop the current holder (regardless of token) and promote the next."""
        async with self._lock:
            if self._holder:
                self._history.appendleft({
                    "container": self._holder["container"],
                    "workload": self._holder["workload"] + " (force-released)",
                    "duration_seconds": time.time() - self._holder["started_at"],
                    "waited_seconds": self._holder["waited_seconds"],
                    "ended_at": time.time(),
                })
                print(f"[broker] force-release: {self._holder['container']}/{self._holder['workload']}", flush=True)
                self._holder = None
                self._promote_next_unlocked()

    def state(self) -> dict:
        return {
            "holder": self._holder and {k: v for k, v in self._holder.items()},
            "queue": [
                {
                    "token": e["token"],
                    "container": e["container"],
                    "workload": e["workload"],
                    "queued_at": e["queued_at"],
                }
                for e in self._queue
            ],
            "history": list(self._history),
            "now": time.time(),
        }


broker = Broker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[broker] startup", flush=True)
    yield
    print("[broker] shutdown", flush=True)


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/acquire")
async def acquire(req: AcquireRequest):
    token, waited = await broker.acquire(req.container, req.workload, req.eta_seconds)
    return {"token": token, "waited_seconds": waited}


@app.delete("/lease/{token}")
async def release(token: str):
    ok = await broker.release(token)
    if not ok:
        raise HTTPException(status_code=404, detail="No such lease")
    return {"ok": True}


@app.delete("/api/holder")
async def force_release_holder():
    """Admin: force-release the current holder. Used by dashboard's kill button."""
    await broker.force_release()
    return {"ok": True}


@app.get("/api/state")
async def get_state():
    return broker.state()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>GPU Broker</title>
<style>
  body { background: #1c1c1e; color: #e8e3d9; font-family: ui-monospace, Menlo, monospace;
         margin: 0; padding: 24px; }
  h1 { font-size: 20px; letter-spacing: -0.5px; margin: 0 0 24px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 2px; color: #aeaeb2;
       margin: 24px 0 8px; }
  .card { background: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px;
          padding: 16px 20px; margin-bottom: 12px; }
  .holder { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  .container { font-weight: 700; color: #c79968; }
  .workload { color: #aeaeb2; }
  .elapsed { font-variant-numeric: tabular-nums; }
  .idle { color: #636366; font-style: italic; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #aeaeb2; font-weight: 600; padding: 6px 8px;
       border-bottom: 1px solid #3a3a3c; font-size: 11px; text-transform: uppercase;
       letter-spacing: 1px; }
  td { padding: 6px 8px; border-bottom: 1px solid #2c2c2e; }
  tr:last-child td { border-bottom: none; }
  .empty { color: #636366; font-style: italic; padding: 8px; }
  button { background: #5a2a2a; color: #e8e3d9; border: 1px solid #7a3a3a;
           border-radius: 6px; padding: 6px 12px; cursor: pointer; font-family: inherit;
           font-size: 12px; }
  button:hover { background: #7a3a3a; }
</style>
</head><body>
<h1>gpu-broker</h1>

<h2>Currently holding</h2>
<div class="card" id="holder">loading…</div>

<h2>Queue</h2>
<div class="card" id="queue">loading…</div>

<h2>Recent</h2>
<div class="card" id="history">loading…</div>

<script>
function fmt(secs) {
  if (secs < 60) return secs.toFixed(1) + 's';
  if (secs < 3600) return (secs/60).toFixed(1) + 'm';
  return (secs/3600).toFixed(2) + 'h';
}
function relTime(ts, now) { return fmt(now - ts); }
async function forceRelease() {
  if (!confirm('Force-release the current holder?')) return;
  await fetch('/api/holder', { method: 'DELETE' });
  refresh();
}
async function refresh() {
  let s;
  try { s = await (await fetch('/api/state')).json(); }
  catch (e) { document.getElementById('holder').textContent = 'broker unreachable'; return; }
  const now = s.now;
  const h = document.getElementById('holder');
  if (s.holder) {
    h.innerHTML =
      '<div class="holder">' +
        '<div>' +
          '<span class="container">' + s.holder.container + '</span> ' +
          '<span class="workload">· ' + s.holder.workload + '</span>' +
          '<div style="font-size:11px;color:#636366;margin-top:4px">' +
            'elapsed <span class="elapsed">' + relTime(s.holder.started_at, now) + '</span>' +
            (s.holder.waited_seconds > 0.5 ? ' · waited ' + fmt(s.holder.waited_seconds) : '') +
          '</div>' +
        '</div>' +
        '<button onclick="forceRelease()">Force release</button>' +
      '</div>';
  } else {
    h.innerHTML = '<span class="idle">GPU is free</span>';
  }
  const q = document.getElementById('queue');
  if (s.queue.length === 0) {
    q.innerHTML = '<div class="empty">no waiters</div>';
  } else {
    q.innerHTML = '<table><thead><tr><th>Pos</th><th>Container</th><th>Workload</th><th>Waited</th></tr></thead><tbody>' +
      s.queue.map((e, i) =>
        '<tr><td>' + (i+1) + '</td>' +
        '<td class="container">' + e.container + '</td>' +
        '<td class="workload">' + e.workload + '</td>' +
        '<td class="elapsed">' + relTime(e.queued_at, now) + '</td></tr>'
      ).join('') + '</tbody></table>';
  }
  const hi = document.getElementById('history');
  if (s.history.length === 0) {
    hi.innerHTML = '<div class="empty">no history yet</div>';
  } else {
    hi.innerHTML = '<table><thead><tr><th>Container</th><th>Workload</th><th>Duration</th><th>Waited</th><th>When</th></tr></thead><tbody>' +
      s.history.map(e =>
        '<tr><td class="container">' + e.container + '</td>' +
        '<td class="workload">' + e.workload + '</td>' +
        '<td class="elapsed">' + fmt(e.duration_seconds) + '</td>' +
        '<td class="elapsed">' + fmt(e.waited_seconds) + '</td>' +
        '<td class="elapsed">' + relTime(e.ended_at, now) + ' ago</td></tr>'
      ).join('') + '</tbody></table>';
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body></html>
"""
