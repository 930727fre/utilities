"""HlsSession: live HLS transcode via NVENC + keyframe-aligned segments.

Replicates Jellyfin's DynamicHlsController + DynamicHlsPlaylistGenerator
flow. Two pieces work together to make seek frame-precise:

  1. **Keyframe-aware segmentation** (DynamicHlsPlaylistGenerator.cs):
     ffprobe extracts every source video keyframe up-front. Segments are
     packed `target_seconds` worth of source between keyframes, so every
     segment boundary IS a keyframe. master.m3u8 has variable EXTINF
     durations reflecting actual keyframe gaps.

  2. **Force keyframes in NVENC output** (-force_key_frames): ffmpeg gets
     the same boundary times so its output has keyframes at exactly the
     same positions. The HLS muxer breaks output segments at those
     keyframes, lining up with the playlist.

Result: when the player seeks to time T, hls.js requests segment K (the
one declared to cover T in master.m3u8). Backend respawns ffmpeg with
`-ss {segments[K].start}` which IS a source keyframe → no snap, no Δ.
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SEGMENT_LENGTH = 6  # seconds; target segment length, actual lengths vary with keyframes

# 24-second gap (Jellyfin's `24 / SegmentLength`): if the requested segment
# is more than this many segments ahead of the current ffmpeg position,
# kill the current process and respawn from the requested position.
GAP_THRESHOLD = 24 // SEGMENT_LENGTH

SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/tmp/live-hls"))
KEYFRAME_CACHE_DIR = SESSION_DIR / "_keyframe_cache"
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
    # Keyframe-aligned segments: list of (start_time_seconds, duration_seconds).
    # Each segment's start_time IS a source video keyframe — so when ffmpeg
    # respawns from segments[K].start, fast seek lands exactly on a keyframe,
    # no Δ snap, no drift. master.m3u8 EXTINF lines reflect these durations.
    segments: list[tuple[float, float]] = field(default_factory=list)
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


def _cache_path_for(source_path: Path) -> Path:
    """Cache file location for source's keyframe data. Keyed by full path
    hash so different wrappers with the same filename don't collide."""
    key = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:16]
    return KEYFRAME_CACHE_DIR / f"{key}.json"


def probe_keyframes(source_path: Path) -> Optional[list[float]]:
    """Extract every video keyframe's PTS time (seconds) via ffprobe. Uses
    Jellyfin's exact incantation — `-skip_frame nokey` makes the demuxer
    walk packet headers without decoding, which keeps the scan fast even
    on multi-hour HEVC sources.

    Cached on disk by source path + size + mtime; first call on a fresh
    source takes 1-15 seconds, subsequent calls are instant.
    """
    try:
        st = source_path.stat()
    except OSError:
        return None
    cache_path = _cache_path_for(source_path)
    # Cache hit
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if data.get("size") == st.st_size and abs(data.get("mtime", 0) - st.st_mtime) < 1:
                return list(data["keyframes"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass  # stale / corrupt → re-probe

    # Cache miss — actually probe.
    try:
        result = subprocess.run(
            ["ffprobe", "-fflags", "+genpts", "-v", "error",
             "-skip_frame", "nokey",
             "-show_entries", "packet=pts_time,flags",
             "-select_streams", "v",
             "-of", "csv=p=0",
             str(source_path)],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[keyframe-probe] ffprobe failed for {source_path.name}: {e}", flush=True)
        return None
    if result.returncode != 0:
        print(f"[keyframe-probe] ffprobe rc={result.returncode}: {result.stderr[:200]}", flush=True)
        return None

    keyframes: list[float] = []
    for line in result.stdout.splitlines():
        # Format: "pts_time,flags" where flags contains "K" for keyframes.
        # With -skip_frame nokey, every emitted packet IS a keyframe, but
        # the K flag check is cheap belt-and-suspenders.
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        if "K" not in parts[1]:
            continue
        try:
            kf = float(parts[0])
        except ValueError:
            continue
        keyframes.append(kf)
    keyframes.sort()

    # Persist for next session on the same source.
    try:
        KEYFRAME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "size": st.st_size,
            "mtime": st.st_mtime,
            "keyframes": keyframes,
        }))
    except OSError as e:
        print(f"[keyframe-probe] cache write failed: {e}", flush=True)

    return keyframes


def compute_segments(keyframes: list[float], total_duration: float,
                     target_seconds: float = SEGMENT_LENGTH) -> list[tuple[float, float]]:
    """Group keyframes into segments aiming for `target_seconds` each.
    Replicates Jellyfin's ComputeSegments. Each output (start, duration)
    has start = a source keyframe, duration = gap to next chosen keyframe.

    Falls back to fixed-length segments if keyframes are missing or sparse
    (e.g. ffprobe failed, or source has bizarre GOP) so playback still works,
    just with the original snap behaviour.
    """
    if not keyframes:
        # Fallback: equal-length segments
        whole = int(total_duration // target_seconds)
        remainder = total_duration - (whole * target_seconds)
        out = [(float(i * target_seconds), float(target_seconds)) for i in range(whole)]
        if remainder > 0.001:
            out.append((float(whole * target_seconds), remainder))
        return out

    # Ensure the keyframe list starts at (or near) 0; some sources don't
    # emit a keyframe with pts=0, but pretty much always have one within
    # the first second.
    if keyframes[0] > 0.1:
        keyframes = [0.0] + keyframes

    segments: list[tuple[float, float]] = []
    last_kf = 0.0
    desired_cut = target_seconds
    for kf in keyframes:
        if kf >= desired_cut:
            segments.append((last_kf, kf - last_kf))
            last_kf = kf
            desired_cut = kf + target_seconds
    # Tail segment runs from last cut to end-of-source.
    if total_duration - last_kf > 0.1:
        segments.append((last_kf, total_duration - last_kf))
    return segments


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


def generate_master_playlist(segments: list[tuple[float, float]]) -> str:
    """VOD playlist with variable-duration segments aligned to keyframes.
    Each segment's EXTINF reflects the actual keyframe gap."""
    if not segments:
        return "#EXTM3U\n#EXT-X-ENDLIST\n"

    # TARGETDURATION must be >= the largest segment duration (rounded up).
    target_duration = max(int(d + 0.999) for _, d in segments)

    lines = [
        "#EXTM3U",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i, (_, duration) in enumerate(segments):
        lines.append(f"#EXTINF:{duration:.6f}, nodesc")
        lines.append(f"seg_{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ── Session lifecycle ─────────────────────────────────────────────────────

def create_session(path: Path) -> HlsSession:
    """Probe duration + keyframes, compute segments, write master.m3u8, return
    session. No ffmpeg is spawned yet — that happens on the first segment
    request. The keyframe probe is cached on disk, so the second time the
    same source is opened, this returns instantly."""
    duration = probe_duration(path)
    if duration is None:
        raise ValueError(f"could not probe duration for {path}")

    keyframes = probe_keyframes(path) or []
    segments = compute_segments(keyframes, duration, target_seconds=SEGMENT_LENGTH)
    if not segments:
        raise ValueError(f"could not compute segments for {path}")

    sid = uuid.uuid4().hex[:8]
    work_dir = SESSION_DIR / sid
    work_dir.mkdir(parents=True, exist_ok=True)

    session = HlsSession(
        sid=sid,
        source_path=path,
        duration_seconds=duration,
        work_dir=work_dir,
        segments=segments,
        is_hdr=probe_is_hdr(path),
    )
    (work_dir / "master.m3u8").write_text(generate_master_playlist(segments))
    print(f"[hls {sid}] created: duration={duration:.1f}s segs={len(segments)} "
          f"keyframes={len(keyframes)} hdr={session.is_hdr}", flush=True)
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

    `start_seconds` is the start of segment `start_seg` from the
    keyframe-aligned segments table — i.e. always a source keyframe, so
    ffmpeg's fast seek lands exactly there with no Δ snap. `-force_key_frames`
    pins output keyframes at every subsequent segment boundary so the HLS
    muxer breaks output where we want, not where NVENC's GOP would.
    """
    start_seconds, _ = session.segments[start_seg]
    # Force keyframes at every boundary from this segment forward. Times are
    # source-time absolute since we use -copyts.
    force_kf_times = ",".join(
        f"{seg_start:.3f}"
        for seg_start, _ in session.segments[start_seg + 1:]
    )
    force_kf_flag = ["-force_key_frames", force_kf_times] if force_kf_times else []

    common_tail = [
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        # Keep timestamps coherent across respawns so the player doesn't
        # see discontinuity gaps at seg boundaries.
        "-copyts", "-avoid_negative_ts", "disabled",
        # Pin output keyframes to our segment boundaries (no-op when this
        # is the last segment of the source).
        *force_kf_flag,
        "-f", "hls",
        # hls_time is an upper hint; force_key_frames + the muxer's
        # split-on-keyframe behaviour determines the real cut points.
        "-hls_time", str(session.segment_length),
        "-hls_list_size", "0",
        "-hls_playlist_type", "vod",
        "-hls_flags", "split_by_time",
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
            "-b:v", "8M", "-profile:v", "high",
            *common_tail,
        ]

    return [
        "ffmpeg", "-y",
        "-ss", f"{start_seconds}",
        "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
        "-i", str(session.source_path),
        "-vf", _SDR_NVENC_VF,
        "-c:v", "h264_nvenc", "-preset", "p4",
        "-b:v", "8M", "-profile:v", "high",
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
