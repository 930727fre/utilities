"""Whisper HTTP client — pre-transcodes source audio to 16 kHz mono AAC
then POSTs to the shared faster-whisper-server. Wrapped in the shared
GPU lock so this consumer doesn't race the marker pipeline for VRAM.

Extracted from tasks.py so callers pass an `is_cancelled` callback
instead of coupling to jobs.json — this module doesn't know or care
about job state, only about "should I still be running."
"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import requests

from gpu_lock import gpu_lock

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")

TRANSCRIBE_TIMEOUT = 4 * 60 * 60   # 4 hours — outer whisper HTTP call cap
FFMPEG_AUDIO_TIMEOUT = 5 * 60      # transcoding a movie to 16kHz mono AAC — ~30-60s typical


def _extract_audio_for_whisper(media_path: Path, out_path: Path) -> None:
    """Transcode `media_path`'s audio to a 16 kHz mono AAC file at
    `out_path`. Raises on ffmpeg failure.

    Whisper's first internal step is exactly this same `-vn -ac 1
    -ar 16000` ffmpeg pass. Doing it client-side instead of letting
    whisper-server's ffmpeg do it server-side means the HTTP body we
    POST is ~30-50 MB regardless of source resolution / video bitrate
    / subtitle-track count, instead of the 1-3 GB the source mkv is.

    Concrete reason this matters: PSA Chernobyl Blu-rays are 2.3-2.5
    GB each (HEVC 1080p video + 11 PGS subtitle tracks). Uploading
    those over the docker bridge to whisper-server hits a sporadic
    "Connection aborted" — root cause unidentified but consistently
    correlated with file size (1.1 GB GoT episodes upload first-try,
    2.5 GB Chernobyl episodes need 3-4 retries). Pre-transcoding
    sidesteps the whole class by keeping uploads small + uniform.

    The output is `.m4a` (AAC in mp4 container). 64 kbps is plenty
    headroom for the 16 kHz mono signal whisper consumes; quality
    parity with letting whisper-server downsample server-side.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-loglevel", "error",
                "-i", str(media_path),
                "-vn",           # drop video stream
                "-ac", "1",      # mono — whisper downmixes anyway
                "-ar", "16000",  # 16 kHz — whisper's internal sample rate
                "-c:a", "aac",
                "-b:a", "64k",
                str(out_path),
            ],
            capture_output=True,
            timeout=FFMPEG_AUDIO_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        raise RuntimeError(f"ffmpeg audio extraction failed: {e}") from e
    if r.returncode != 0:
        stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg audio extraction returned {r.returncode}: {stderr[-300:]}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg audio extraction produced empty output")


def run_whisper(
    media_path: Path,
    srt_path: Path,
    *,
    is_cancelled: Optional[Callable[[], bool]] = None,
    lock_owner: str = "transcribe-app",
    lock_reason: str = "whisper",
) -> None:
    """Transcode `media_path` to a small audio file, POST it to the
    shared whisper service, write the returned SRT to `srt_path`.

    Holds the cross-container GPU lock around the HTTP call so this
    consumer doesn't race marker-pipeline for VRAM. faster-whisper-server
    has its own internal queue for whisper-only contention.

    Audio extraction runs OUTSIDE the GPU lock — it's pure CPU work
    (ffmpeg), no reason to block other GPU consumers while we transcode.
    See `_extract_audio_for_whisper` for why we extract client-side.

    `is_cancelled` is polled at safe cancellation points (before ffmpeg,
    before HTTP under lock); returning True short-circuits the call. The
    callback lets the caller (jobs.json / user-triggered abort) unwind
    without this module knowing about job state.

    Raises RuntimeError on ffmpeg or HTTP / whisper-server failure.
    """
    if is_cancelled and is_cancelled():
        return

    with tempfile.TemporaryDirectory(prefix="whisper-audio-") as tmpdir:
        audio_path = Path(tmpdir) / "audio.m4a"
        _extract_audio_for_whisper(media_path, audio_path)

        if is_cancelled and is_cancelled():
            return

        with gpu_lock(lock_owner, lock_reason):
            # Re-check after acquiring the lock (could have been cancelled while we waited).
            if is_cancelled and is_cancelled():
                return

            try:
                with open(audio_path, "rb") as f:
                    resp = requests.post(
                        f"{WHISPER_URL}/v1/audio/transcriptions",
                        files={"file": (audio_path.name, f, "audio/mp4")},
                        data={
                            "model": "whisper-1",  # ignored by fedirz; uses WHISPER__MODEL
                            "response_format": "srt",
                            "temperature": "0",
                            # silero VAD strips silence/music sections before whisper
                            # processes — kills the hallucination loops ("CastingWords",
                            # "Thank you" etc.) that whisper falls into on long files
                            # with extended non-speech audio (movies, podcasts with
                            # instrumental segments). condition_on_previous_text isn't
                            # exposed by fedirz, so VAD is the only knob we have.
                            "vad_filter": "true",
                        },
                        timeout=(10, TRANSCRIBE_TIMEOUT),
                    )
            except requests.RequestException as e:
                raise RuntimeError(f"Whisper service call failed: {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(f"Whisper service returned {resp.status_code}: {resp.text[:300]}")

    srt_text = resp.text
    if not srt_text.strip():
        raise RuntimeError("Whisper service returned empty SRT")

    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(srt_text, encoding="utf-8")
