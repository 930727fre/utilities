# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp>=3.9",
#     "pyobjc-framework-Quartz>=10",
# ]
# ///
"""iPhone-as-trackpad server for macOS.

Serves the sibling index.html and a WebSocket endpoint (/ws) on the
same port. Phone Safari connects, touch events → synthetic mouse
events via Quartz.

macOS Accessibility permission is REQUIRED for the Python interpreter
(System Settings → Privacy & Security → Accessibility). Without it,
Quartz.CGEventPost silently returns and the cursor doesn't move. If
mouse movement works but tap-click doesn't, that's still a permission
issue — grant Terminal (or whichever app spawned Python).

Run:
    uv run trackpad.py [--port 8080] [--host 0.0.0.0]

Dependencies are declared inline (PEP 723) — uv builds an ephemeral
venv on first run, caches it, reuses it on later runs. No manual
venv, no requirements.txt, no pyproject.toml.

Connect from iPhone (both devices on the same Tailscale tailnet):
    http://<mac-hostname>:8080/
"""
import argparse
import asyncio
import json
import math
import time
from pathlib import Path

from aiohttp import web, WSMsgType
import Quartz

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"

# Cursor acceleration curve. macOS's own pointer ballistics amplify by
# input velocity — slow touches map near 1:1 for precision, fast flicks
# ramp up to cross the screen quickly. A single scalar can't be both,
# so per event: gain = min(BASE + ACCEL * touch_velocity, MAX), where
# velocity is hypot(dx, dy) / dt-since-prior-event in touch-pixels/sec.
# Rough tuning: bump ACCEL if the cursor feels sluggish crossing a
# large monitor; drop BASE if fine-target work feels twitchy.
POINTER_BASE = 0.6
POINTER_ACCEL = 0.0015
POINTER_MAX = 7.0

# Scroll wheel acceleration curve — same shape as the pointer curve
# above. Slow finger scrolls read as precise line-by-line, fast flicks
# jump multiple screens. Touch deltas are in phone-screen pixels;
# macOS wheel wants pixels for kCGScrollEventUnitPixel.
SCROLL_BASE = 0.8
SCROLL_ACCEL = 0.0015
SCROLL_MAX = 4.0

# Momentum scroll parameters. When the client's 2-finger scroll gesture
# ends with non-trivial lift-off velocity, keep emitting scroll events
# with exponentially-decaying velocity until it drops below STOP. DECAY
# is per-tick; at 60Hz, 0.94 keeps ~30% of v0 after ~0.33s and dies out
# around 1s — matches native macOS momentum feel closely enough. The
# same scroll gain (BASE/ACCEL/MAX above) is applied per tick so
# momentum inherits the same "amount of scroll per unit finger motion"
# the active gesture had — otherwise crossing the lift-off boundary
# feels like the scroll suddenly weakens.
MOMENTUM_TICK = 1.0 / 60
MOMENTUM_DECAY = 0.94
MOMENTUM_STOP = 60.0

# Main display bounds in Quartz global coordinate space. Cached at
# import time — good enough since the app runs foreground and users
# rarely hot-plug displays mid-session. Used to clamp the cursor to
# the edge instead of letting Quartz accumulate off-screen state.
_bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
SCREEN_W = float(_bounds.size.width)
SCREEN_H = float(_bounds.size.height)


def _current_pos() -> tuple[float, float]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


_last_move_time: float | None = None


def move_relative(dx: float, dy: float) -> None:
    global _last_move_time
    now = time.monotonic()
    # First event of the session (or first after the module state was
    # reset) has no prior timestamp to derive velocity from — fall back
    # to BASE gain. Floor dt at 1ms so two events landing on the same
    # scheduler tick don't divide by ~0 and produce infinite velocity.
    if _last_move_time is None:
        gain = POINTER_BASE
    else:
        dt = max(now - _last_move_time, 1e-3)
        speed = math.hypot(dx, dy) / dt
        gain = min(POINTER_BASE + POINTER_ACCEL * speed, POINTER_MAX)
    _last_move_time = now
    mdx = dx * gain
    mdy = dy * gain
    x, y = _current_pos()
    x += mdx
    y += mdy
    # Explicit clamp to display bounds. Without this, Quartz internally
    # accumulates the off-screen overshoot (CGEventGetLocation on the
    # next tick returns an out-of-bounds value even though the cursor is
    # visually pinned at the edge). Subsequent deltas keep piling onto
    # the ghost position; from macOS's dock-reveal detector's view, the
    # cursor never actually *sits* at the edge — it keeps sliding
    # further past — so the "cursor rests against edge" trigger never
    # fires. Clamping keeps _current_pos honest and lets the edge state
    # persist across ticks so the dock reveal fires normally.
    x = max(0.0, min(x, SCREEN_W - 1.0))
    y = max(0.0, min(y, SCREEN_H - 1.0))
    ev = Quartz.CGEventCreateMouseEvent(
        None,
        Quartz.kCGEventMouseMoved,
        (x, y),
        Quartz.kCGMouseButtonLeft,
    )
    # Delta fields signal "user is pushing" independently of position.
    # Setting both integer and double forms is belt-and-suspenders —
    # different consumers inside macOS read from different fields; see
    # Barrier's OSXScreen.mm which sets both after debugging 3D-app
    # issues.
    idx, idy = int(round(mdx)), int(round(mdy))
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaX, idx)
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaY, idy)
    Quartz.CGEventSetDoubleValueField(ev, Quartz.kCGMouseEventDeltaX, mdx)
    Quartz.CGEventSetDoubleValueField(ev, Quartz.kCGMouseEventDeltaY, mdy)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _click(button: int) -> None:
    x, y = _current_pos()
    if button == 0:
        down, up = Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp
        btn = Quartz.kCGMouseButtonLeft
    else:
        down, up = Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp
        btn = Quartz.kCGMouseButtonRight
    for et in (down, up):
        ev = Quartz.CGEventCreateMouseEvent(None, et, (x, y), btn)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


_last_scroll_time: float | None = None


def scroll(dy: float, dx: float) -> None:
    global _last_scroll_time
    now = time.monotonic()
    if _last_scroll_time is None:
        gain = SCROLL_BASE
    else:
        dt = max(now - _last_scroll_time, 1e-3)
        speed = math.hypot(dx, dy) / dt
        gain = min(SCROLL_BASE + SCROLL_ACCEL * speed, SCROLL_MAX)
    _last_scroll_time = now
    # Wheel Y positive = scroll up (content moves down). Phone swipe
    # down (touch dy > 0) should scroll document down = wheel negative,
    # so we flip the sign. Same story for X.
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None,
        Quartz.kCGScrollEventUnitPixel,
        2,
        int(-dy * gain),
        int(-dx * gain),
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


_momentum_task: asyncio.Task | None = None


def cancel_momentum() -> None:
    global _momentum_task
    t = _momentum_task
    _momentum_task = None
    if t is not None and not t.done():
        t.cancel()


async def _run_momentum(vx: float, vy: float) -> None:
    try:
        while math.hypot(vx, vy) > MOMENTUM_STOP:
            speed = math.hypot(vx, vy)
            gain = min(SCROLL_BASE + SCROLL_ACCEL * speed, SCROLL_MAX)
            dx = vx * MOMENTUM_TICK * gain
            dy = vy * MOMENTUM_TICK * gain
            ev = Quartz.CGEventCreateScrollWheelEvent(
                None,
                Quartz.kCGScrollEventUnitPixel,
                2,
                int(-dy),
                int(-dx),
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            vx *= MOMENTUM_DECAY
            vy *= MOMENTUM_DECAY
            await asyncio.sleep(MOMENTUM_TICK)
    except asyncio.CancelledError:
        pass


def start_momentum(vx: float, vy: float) -> None:
    global _momentum_task
    cancel_momentum()
    _momentum_task = asyncio.create_task(_run_momentum(vx, vy))


async def index(_req):
    return web.FileResponse(INDEX_HTML)


async def websocket_handler(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    peer = req.remote or "?"
    print(f"[trackpad] connect: {peer}", flush=True)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            kind = data.get("t")
            # Any user-initiated event kills in-flight momentum, matching
            # native trackpad behaviour where touching the surface again
            # stops the coast. scroll_end starts a new momentum run so
            # cancel_momentum() runs inside start_momentum() there too.
            if kind == "move":
                cancel_momentum()
                move_relative(float(data.get("dx", 0)), float(data.get("dy", 0)))
            elif kind == "click":
                cancel_momentum()
                _click(1 if data.get("b") == "right" else 0)
            elif kind == "scroll":
                cancel_momentum()
                scroll(float(data.get("dy", 0)), float(data.get("dx", 0)))
            elif kind == "scroll_end":
                start_momentum(float(data.get("vx", 0)), float(data.get("vy", 0)))
    finally:
        print(f"[trackpad] disconnect: {peer}", flush=True)
    return ws


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket_handler)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"[trackpad] serving http://{args.host}:{args.port}/", flush=True)
    print(f"[trackpad] iPhone: http://<mac tailscale name>:{args.port}/", flush=True)
    web.run_app(build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
