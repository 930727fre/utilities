"""Pre-compute HLS for every bt video so the browser player has a static
playlist + segments ready to FileResponse — no live transcoding, no
session lifecycle, no Jellyfin.

Driven by main.py's scan loop. For each video in bt/, ensure
`data/derived/<wrapper>/<stem>/master.m3u8` (+ seg_*.ts) is complete.
Completion sentinel is the playlist's own `#EXT-X-ENDLIST` tag (written
by ffmpeg only after the whole transcode succeeds), so we don't need a
separate `.complete` file.

Crash recovery via an in-memory `_hls_jobs` registry: a process dying
mid-write leaves a derived dir whose master.m3u8 lacks `#EXT-X-ENDLIST`,
which `ensure()` detects and re-queues after cleaning the debris.
"""
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DERIVED_ROOT = Path("/app/data/derived")

# Single worker — one ffmpeg at a time. Multi-process transcoding contends
# for CPU and adds noisy I/O without finishing faster overall on this host.
hls_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hls-worker")

_hls_jobs: dict[str, subprocess.Popen] = {}  # str(derived_dir) → ffmpeg proc
_lock = threading.Lock()


def is_complete(derived_dir: Path) -> bool:
    """True iff master.m3u8 exists AND ends with #EXT-X-ENDLIST (the ffmpeg
    HLS muxer only emits this after the whole transcode finishes)."""
    m = derived_dir / "master.m3u8"
    if not m.exists():
        return False
    try:
        return b"#EXT-X-ENDLIST" in m.read_bytes()
    except OSError:
        return False


def is_in_flight(derived_dir: Path) -> bool:
    """True iff this derived_dir has a live ffmpeg process recorded in the
    registry."""
    key = str(derived_dir)
    with _lock:
        proc = _hls_jobs.get(key)
    return proc is not None and proc.poll() is None


def _transcode(video: Path, derived_dir: Path) -> None:
    """Sync transcode — runs inside hls_executor thread.

    Validated argv (see HLS_PLAN.md): libx264 + yuv420p + AAC stereo,
    6-second HLS segments as a VOD playlist. Tested cleanly on Sopranos
    S05 HEVC 10-bit (the bit-depth coerce needs -pix_fmt yuv420p), Wire
    SDR, Chernobyl, Spider-Verse, modern YIFY rips.
    """
    derived_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-i", str(video),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-b:v", "8M", "-profile:v", "high", "-level", "4.1",
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(derived_dir / "seg_%d.ts"),
        str(derived_dir / "master.m3u8"),
    ]
    print(f"[hls] start {video.name} → {derived_dir}", flush=True)
    # stderr MUST be drained continuously — ffmpeg writes a progress line
    # every second and the 64 KB kernel pipe buffer fills around 15-25
    # minutes in, at which point the next stderr write blocks ffmpeg
    # entirely. communicate() reads the pipe in parallel with proc.wait(),
    # so the buffer never backs up.
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    key = str(derived_dir)
    with _lock:
        _hls_jobs[key] = proc
    try:
        _, stderr_bytes = proc.communicate()
        if proc.returncode == 0 and is_complete(derived_dir):
            print(f"[hls] done {video.name}", flush=True)
        else:
            err = stderr_bytes.decode("utf-8", errors="replace").strip().splitlines()[-3:]
            print(f"[hls] FAILED rc={proc.returncode} {video.name}: {' | '.join(err)}",
                  flush=True)
    finally:
        with _lock:
            _hls_jobs.pop(key, None)


def _clean_debris(derived_dir: Path) -> None:
    """Remove stale HLS segments + playlist before re-queuing. Other files
    in derived_dir (english.srt, annotated.srt, zh.srt) are left alone."""
    (derived_dir / "master.m3u8").unlink(missing_ok=True)
    for seg in derived_dir.glob("seg_*.ts"):
        try:
            seg.unlink()
        except OSError:
            pass


def ensure(video: Path, derived_dir: Path) -> None:
    """Idempotent: submit a transcode if needed, no-op otherwise.

    States:
      complete (ENDLIST present)               → no-op
      in-flight (live ffmpeg in registry)      → no-op
      partial debris (no ENDLIST, dead proc)   → clean + queue
      fresh                                    → queue
    """
    if is_complete(derived_dir):
        return
    if is_in_flight(derived_dir):
        return
    # Anything else is partial / fresh → make sure the slate is clean,
    # then queue.
    if derived_dir.exists() and any(derived_dir.glob("seg_*.ts")):
        _clean_debris(derived_dir)
    hls_executor.submit(_transcode, video, derived_dir)
