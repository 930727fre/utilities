"""HlsSession: live HLS transcode via NVENC + absolute segment numbering.

Replicates the segment-request decision logic from Jellyfin's
DynamicHlsController.GetDynamicSegment without the multi-client /
multi-codec / DLNA / scanner ceremony.

The key insight (from Jellyfin): the master playlist is generated up-front
from the source duration. Segment K corresponds to source time
[K * segment_length, (K+1) * segment_length), regardless of which ffmpeg
process produced it. When a new ffmpeg is spawned (initial play or seek),
it gets `-start_number K` so its output filenames continue from K — the
player sees a continuous, consistent sequence even across respawns.
"""
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SEGMENT_LENGTH = 6  # seconds; matches Jellyfin's default

# 24-second gap (Jellyfin's `24 / SegmentLength`): if the requested segment
# is more than this many segments ahead of the current ffmpeg position,
# kill the current process and respawn from the requested position.
GAP_THRESHOLD = 24 // SEGMENT_LENGTH

SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/tmp/live-hls"))
MEDIA_ROOTS = [Path(p.strip()) for p in os.environ.get("MEDIA_ROOTS", "/media").split(",")]
SESSION_IDLE_TIMEOUT = float(os.environ.get("SESSION_IDLE_TIMEOUT", "60"))

# How long /api/{sid}/seg_N.ts waits for a not-yet-existing segment file
# before returning 504. NVENC cold start on a long HEVC source can hit
# 15-30 seconds for far-in seeks (decoder has to keyframe-snap from the
# requested -ss before any output appears). 60s gives plenty of slack.
WAIT_TIMEOUT = 60.0


@dataclass
class HlsSession:
    sid: str
    source_path: Path
    duration_seconds: float
    work_dir: Path
    segment_length: int = SEGMENT_LENGTH
    # Detected once at session creation. Drives NVENC vs CPU-tonemap argv
    # selection in _ffmpeg_argv; never changes mid-session.
    is_hdr: bool = False
    proc: Optional[subprocess.Popen] = None
    proc_start_seg: int = 0
    last_request_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_request_at = time.time()

    def is_idle(self, timeout: float = SESSION_IDLE_TIMEOUT) -> bool:
        return (time.time() - self.last_request_at) > timeout


# ── Validation ────────────────────────────────────────────────────────────

def validate_path(path_str: str) -> Path:
    """Reject paths outside MEDIA_ROOTS. Resolves symlinks before checking
    so a symlink trick can't escape the mount."""
    path = Path(path_str).resolve()
    for root in MEDIA_ROOTS:
        try:
            path.relative_to(root.resolve())
            return path
        except ValueError:
            continue
    raise ValueError(f"path not under any media root: {path_str}")


# ── ffprobe / playlist ────────────────────────────────────────────────────

def probe_duration(path: Path) -> Optional[float]:
    """Source duration in seconds via ffprobe. Returns None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        d = float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None
    return d if d > 0 else None


def probe_is_hdr(path: Path) -> bool:
    """True iff the source video's transfer characteristic is PQ
    (`smpte2084`, HDR10) or HLG (`arib-std-b67`). NVENC + no tone-mapping
    would emit washed-out / over-bright output for these; we route them
    through the CPU `tonemap=hable` path instead. SDR (BT.709 / unknown)
    returns False."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=nw=1:nk=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    transfer = (result.stdout or "").strip().lower()
    return transfer in {"smpte2084", "arib-std-b67"}


def generate_master_playlist(duration: float, segment_length: int = SEGMENT_LENGTH) -> str:
    """VOD playlist listing every segment up-front. Player sees the full
    timeline; segments are populated on demand by ffmpeg."""
    whole = int(duration // segment_length)
    remainder = duration - (whole * segment_length)

    lines = [
        "#EXTM3U",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{segment_length}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i in range(whole):
        lines.append(f"#EXTINF:{float(segment_length):.6f}, nodesc")
        lines.append(f"seg_{i}.ts")
    if remainder > 0.001:
        lines.append(f"#EXTINF:{remainder:.6f}, nodesc")
        lines.append(f"seg_{whole}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ── Session lifecycle ─────────────────────────────────────────────────────

def create_session(path: Path) -> HlsSession:
    """Probe duration, mkdir work_dir, write master.m3u8, return session.
    No ffmpeg is spawned yet — that happens on the first segment request."""
    duration = probe_duration(path)
    if duration is None:
        raise ValueError(f"could not probe duration for {path}")

    sid = uuid.uuid4().hex[:8]
    work_dir = SESSION_DIR / sid
    work_dir.mkdir(parents=True, exist_ok=True)

    session = HlsSession(
        sid=sid,
        source_path=path,
        duration_seconds=duration,
        work_dir=work_dir,
        is_hdr=probe_is_hdr(path),
    )
    (work_dir / "master.m3u8").write_text(
        generate_master_playlist(duration, session.segment_length)
    )
    return session


def destroy_session(session: HlsSession) -> None:
    """Kill ffmpeg, rmtree work_dir. Called explicitly via DELETE and by
    the idle GC loop. Safe to call on a session that's already partially
    cleaned up."""
    _kill(session)
    try:
        shutil.rmtree(session.work_dir, ignore_errors=True)
    except OSError:
        pass


# ── ffmpeg process management ─────────────────────────────────────────────

_HDR_TONEMAP_VF = (
    # PQ/HLG → linear light → BT.709 primaries → Hable tone-mapping → BT.709
    # SDR. Same chain Jellyfin's CPU tone-map path uses. Slap a scale at
    # the end so 4K HDR gets downscaled to 1080p like everything else.
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=hable,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "scale=-2:1080,"
    "format=yuv420p"
)

# Cap output at 1080p — user doesn't care about 4K detail, this keeps
# segment sizes / NVENC throughput tractable. NV12 is the 8-bit format
# h264_nvenc requires; the implicit-from-source 10-bit gets coerced here.
_SDR_NVENC_VF = "scale_cuda=-2:1080:format=nv12"


def _ffmpeg_argv(session: HlsSession, start_seg: int) -> list[str]:
    """Build the ffmpeg argv. SDR sources go through NVENC (fast); HDR
    sources go through CPU libx264 with Hable tone-mapping (slow but
    correct — NVENC has no clean tone-map path).

    Segment filenames are numbered from `start_seg` onwards so the player
    sees a continuous sequence across respawns. See PLAN.md for the
    absolute segment numbering rationale.
    """
    start_seconds = start_seg * session.segment_length
    common_tail = [
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        # Keep timestamps coherent across respawns so the player doesn't
        # see discontinuity gaps at seg boundaries.
        "-copyts", "-avoid_negative_ts", "disabled",
        "-f", "hls",
        "-hls_time", str(session.segment_length),
        "-hls_list_size", "0",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(session.work_dir / "seg_%d.ts"),
        "-start_number", str(start_seg),
        str(session.work_dir / "internal.m3u8"),
    ]

    if session.is_hdr:
        return [
            "ffmpeg", "-y",
            "-ss", f"{start_seconds}",
            "-i", str(session.source_path),
            "-vf", _HDR_TONEMAP_VF,
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "8M", "-profile:v", "high", "-level", "4.1",
            *common_tail,
        ]

    return [
        "ffmpeg", "-y",
        "-ss", f"{start_seconds}",
        "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
        "-i", str(session.source_path),
        "-vf", _SDR_NVENC_VF,
        "-c:v", "h264_nvenc", "-preset", "p4",
        "-b:v", "8M", "-profile:v", "high", "-level", "4.1",
        *common_tail,
    ]


def _spawn(session: HlsSession, start_seg: int) -> None:
    """Spawn ffmpeg from the given segment index. Stderr goes to a per-run
    log file (not a PIPE) so the kernel can't backpressure ffmpeg via a
    full buffer."""
    args = _ffmpeg_argv(session, start_seg)
    log_path = session.work_dir / f"ffmpeg.{start_seg}.log"
    log_fp = open(log_path, "wb")
    session.proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=log_fp,
    )
    session.proc_start_seg = start_seg
    path_tag = "HDR/CPU" if session.is_hdr else "NVENC"
    print(f"[hls {session.sid}] spawn pid={session.proc.pid} from seg={start_seg} [{path_tag}]",
          flush=True)


def _kill(session: HlsSession) -> None:
    """SIGTERM → wait 2s → SIGKILL fallback. No-op if proc is already gone."""
    proc = session.proc
    if proc is None or proc.poll() is not None:
        session.proc = None
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    except Exception:
        pass
    session.proc = None


# ── Segment request handling ──────────────────────────────────────────────

def current_ffmpeg_index(session: HlsSession) -> Optional[int]:
    """Jellyfin's GetCurrentTranscodingIndex: find the newest seg_*.ts in
    the work dir and parse its index from the filename. None if no ffmpeg
    is live or no segments exist yet."""
    proc = session.proc
    if proc is None or proc.poll() is not None:
        return None
    try:
        segs = list(session.work_dir.glob("seg_*.ts"))
    except OSError:
        return None
    if not segs:
        return None
    newest = max(segs, key=lambda p: p.stat().st_mtime)
    try:
        return int(newest.stem.removeprefix("seg_"))
    except ValueError:
        return None


def _clean_segments_from(session: HlsSession, start_seg: int) -> None:
    """Delete any seg_K.ts where K >= start_seg. Called before respawning
    from start_seg so we don't serve stale data the prior ffmpeg wrote."""
    try:
        segs = list(session.work_dir.glob("seg_*.ts"))
    except OSError:
        return
    for seg_path in segs:
        try:
            n = int(seg_path.stem.removeprefix("seg_"))
        except ValueError:
            continue
        if n >= start_seg:
            try:
                seg_path.unlink()
            except OSError:
                pass


def _wait_for_seg(session: HlsSession, seg_path: Path,
                  timeout: float = WAIT_TIMEOUT) -> Optional[Path]:
    """Block until seg_path exists (and isn't a partial 0-byte file),
    OR ffmpeg exits, OR timeout. Returns the path on success, None on
    failure."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if seg_path.exists() and seg_path.stat().st_size > 0:
                return seg_path
        except OSError:
            pass
        proc = session.proc
        if proc is not None and proc.poll() is not None:
            # ffmpeg has exited. Give the filesystem one more shot then bail.
            time.sleep(0.1)
            try:
                if seg_path.exists() and seg_path.stat().st_size > 0:
                    return seg_path
            except OSError:
                pass
            return None
        time.sleep(0.05)
    return None


def serve_segment(session: HlsSession, seg: int) -> Optional[Path]:
    """Return the path to the requested segment, or None on timeout / proc
    death. Implements the three-tier decision from Jellyfin:

      1. If the file is already on disk, serve it.
      2. Under the session lock, look at the current ffmpeg position.
         Decide whether to respawn (no proc, dead proc, seeking backwards,
         seeking too far forward) or just wait.
      3. Block until the file appears or ffmpeg dies.
    """
    session.touch()
    seg_path = session.work_dir / f"seg_{seg}.ts"

    if seg_path.exists():
        return seg_path

    with session.lock:
        if seg_path.exists():
            return seg_path

        current_idx = current_ffmpeg_index(session)
        proc = session.proc

        respawn = (
            proc is None
            or proc.poll() is not None
            or current_idx is None
            or seg < current_idx
            or (seg - current_idx) > GAP_THRESHOLD
        )

        if respawn:
            _kill(session)
            _clean_segments_from(session, seg)
            _spawn(session, start_seg=seg)
        # else: ffmpeg is close to or just behind seg; the wait below
        # will return as soon as ffmpeg catches up.

    result = _wait_for_seg(session, seg_path)
    if result is None:
        cur = current_ffmpeg_index(session)
        print(f"[hls {session.sid}] TIMEOUT waiting for seg_{seg} "
              f"(current_idx={cur}, start_seg={session.proc_start_seg})", flush=True)
    return result
