"""Sidecar file that records pipeline failure for a video.

One canonical sidecar type:

  <stem>.pipeline-failed    — canonical was NOT written; the scan loop
                              should stop re-enqueueing this file until
                              the user retries. Body format:
                                  <kind>: <reason>
                              e.g.
                                  whisper polluted: 8.7% cues affected...
                                  annotate failed: sonnet timeout
                                  pipeline crashed: TypeError('x')

`kind` is a short label chosen by the caller; it appears in the
Telegram summary and the UI tooltip. It's purely informational —
the scan loop treats all kinds identically.

Filename, not extension, is the state signal — sidecar is
extension-less so Jellyfin / Infuse never try to load it as a
subtitle.
"""
from pathlib import Path
from typing import Optional

_PIPELINE_FAILED_SUFFIX = ".pipeline-failed"


def _short(msg: str) -> str:
    """Collapse whitespace + cap so a multi-line traceback doesn't
    fill the sidecar with junk."""
    return msg.replace("\n", " ").replace("\r", " ").strip()[:500]


def pipeline_failed_path(video: Path) -> Path:
    """Path of the pipeline-failed sidecar for a given video."""
    return video.with_suffix(_PIPELINE_FAILED_SUFFIX)


def stamp_pipeline_failed(video: Path, kind: str, reason: str) -> None:
    """Write a `<stem>.pipeline-failed` sidecar so the scan loop stops
    retrying. `kind` is a short label (`whisper polluted`, `annotate
    failed`, `pipeline crashed`, etc.) prepended to `reason` in the body."""
    p = pipeline_failed_path(video)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{_short(kind)}: {_short(reason)}\n", encoding="utf-8")


def all_failure_sidecar_paths(video: Path) -> list[Path]:
    """Every sidecar path that could indicate this video has failed.
    Currently just one; kept as a helper so callers don't need to know
    the single-sidecar invariant."""
    return [pipeline_failed_path(video)]


def any_failure_sidecar_with_kind(
    video: Path,
) -> Optional[tuple[str, str]]:
    """Return `(kind, reason)` from the pipeline-failed sidecar, or
    None if the video is not in a failed state. Body format is
    `<kind>: <reason>`; body missing the colon (should not happen with
    stamp_pipeline_failed but defensive) falls back to `("failed", body)`."""
    p = pipeline_failed_path(video)
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if ":" in raw:
        kind, _, reason = raw.partition(":")
        return kind.strip(), reason.strip()
    return "failed", raw


def any_failure_reason(video: Path) -> Optional[str]:
    """Convenience: `<kind>: <reason>` string for display, or None if
    the video isn't in a failed state."""
    got = any_failure_sidecar_with_kind(video)
    if got is None:
        return None
    kind, reason = got
    return f"{kind}: {reason}" if reason else kind
