"""iPhone-as-trackpad server for macOS.

Serves the sibling index.html and a WebSocket endpoint (/ws) on the
same port. Phone Safari connects, touch events → synthetic mouse
events via Quartz.

macOS Accessibility permission is REQUIRED for the Python interpreter
(System Settings → Privacy & Security → Accessibility). Without it,
Quartz.CGEventPost silently returns and the cursor doesn't move. If
mouse movement works but tap-click doesn't, that's still a permission
issue — grant Terminal (or whichever app spawned Python).

Deps:
    pip install aiohttp pyobjc-framework-Quartz

Run:
    python3 trackpad.py [--port 8080] [--host 0.0.0.0]

Connect from iPhone (both devices on the same Tailscale tailnet):
    http://<mac-hostname>:8080/
"""
import argparse
import asyncio
import json
from pathlib import Path

from aiohttp import web, WSMsgType
import Quartz

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"

# Cursor motion scale. 1.5 feels close to Apple's built-in trackpad
# with default settings; bump for larger monitors, drop for precision
# work. Applied to raw touch deltas coming off the phone.
POINTER_SPEED = 1.5

# Scroll wheel scale. Touch deltas are in phone screen pixels, macOS
# wheel deltas want pixel units for kCGScrollEventUnitPixel — the 0.4
# multiplier tames a naturally-scrolling swipe into feeling right.
SCROLL_SPEED = 1.2

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


def move_relative(dx: float, dy: float) -> None:
    mdx = dx * POINTER_SPEED
    mdy = dy * POINTER_SPEED
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


def scroll(dy: float, dx: float) -> None:
    # Wheel Y positive = scroll up (content moves down). Phone swipe
    # down (touch dy > 0) should scroll document down = wheel negative,
    # so we flip the sign. Same story for X.
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None,
        Quartz.kCGScrollEventUnitPixel,
        2,
        int(-dy * SCROLL_SPEED),
        int(-dx * SCROLL_SPEED),
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


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
            if kind == "move":
                move_relative(float(data.get("dx", 0)), float(data.get("dy", 0)))
            elif kind == "click":
                _click(1 if data.get("b") == "right" else 0)
            elif kind == "scroll":
                scroll(float(data.get("dy", 0)), float(data.get("dx", 0)))
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
