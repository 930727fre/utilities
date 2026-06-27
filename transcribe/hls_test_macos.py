#!/usr/bin/env python3
"""
Quick HLS test harness — verifies that the PLAN.md ffmpeg argv produces
output that plays cleanly in Safari (native HLS) and Chrome (via hls.js).

Usage:
    python3 hls_test_macos.py <video> [<video> ...]
    python3 hls_test_macos.py --full <video>          # don't cap at 10 min
    python3 hls_test_macos.py --port 8765 <video>

Transcodes each video to /tmp/hls_test/<stem>/, writes an index.html that
picks the right player engine, and serves the dir on localhost.

Requires: python3.10+, ffmpeg in PATH.
"""
from __future__ import annotations

import argparse
import http.server
import socketserver
import subprocess
import sys
import urllib.parse
from pathlib import Path

INDEX_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HLS test</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<style>
body { font-family: -apple-system, sans-serif; margin: 20px;
       background: #1c1c1e; color: #e8e3d9; }
select { font-size: 15px; padding: 8px; margin-bottom: 12px;
         background: #2c2c2e; color: #e8e3d9; border: 1px solid #3a3a3c;
         border-radius: 6px; }
video { width: 100%; max-width: 960px; background: #000; border-radius: 6px; }
.hint { color: #aeaeb2; font-size: 13px; margin-top: 8px; }
</style></head><body>
<h2>HLS test</h2>
<select id="src">__OPTIONS__</select><br>
<video id="v" controls></video>
<p class="hint">Engine: <span id="engine"></span></p>
<script>
const v = document.getElementById('v'), sel = document.getElementById('src');
const eng = document.getElementById('engine');
let hls = null;
function load(url) {
  if (hls) { hls.destroy(); hls = null; }
  if (v.canPlayType('application/vnd.apple.mpegurl')) {
    v.src = url; eng.textContent = 'Safari native HLS';
  } else if (Hls.isSupported()) {
    hls = new Hls(); hls.loadSource(url); hls.attachMedia(v);
    eng.textContent = 'hls.js';
  } else { eng.textContent = 'no HLS support'; }
}
sel.addEventListener('change', () => load(sel.value));
if (sel.options.length) load(sel.value);
</script></body></html>
"""

FFMPEG_TAIL = [
    "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-b:v", "8M", "-profile:v", "high", "-level", "4.1",
    "-preset", "veryfast",
    "-c:a", "aac", "-b:a", "192k", "-ac", "2",
    "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
    "-hls_playlist_type", "vod",
]


def transcode(video: Path, out_dir: Path, duration_seconds: int | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-i", str(video)]
    if duration_seconds:
        args += ["-t", str(duration_seconds)]
    args += FFMPEG_TAIL + [
        "-hls_segment_filename", str(out_dir / "seg_%d.ts"),
        str(out_dir / "master.m3u8"),
    ]
    print(f"[transcode] {video.name} → {out_dir}")
    subprocess.run(args, check=True)


def write_index(cache_root: Path) -> None:
    opts = []
    for d in sorted(cache_root.iterdir()):
        if d.is_dir() and (d / "master.m3u8").exists():
            url = f"./{urllib.parse.quote(d.name)}/master.m3u8"
            opts.append(f'<option value="{url}">{d.name}</option>')
    (cache_root / "index.html").write_text(
        INDEX_HTML.replace("__OPTIONS__", "\n".join(opts))
    )


def serve(cache_root: Path, port: int) -> None:
    write_index(cache_root)
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(cache_root), **kw
    )
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), handler) as srv:
        print(f"\nOpen → http://localhost:{port}/")
        print("Same URL works in Safari + Chrome (page picks the right engine).")
        print("Ctrl-C to stop.\n")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[shutdown]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+", type=Path)
    ap.add_argument("--full", action="store_true",
                    help="transcode full file (default: first 10 minutes)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--cache", type=Path, default=Path("/tmp/hls_test"))
    args = ap.parse_args()

    duration = None if args.full else 600
    args.cache.mkdir(parents=True, exist_ok=True)

    for video in args.videos:
        if not video.exists():
            sys.exit(f"ERROR: {video} not found")
        transcode(video, args.cache / video.stem, duration)

    serve(args.cache, args.port)


if __name__ == "__main__":
    main()
