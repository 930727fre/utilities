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

Legacy sidecars still on disk from earlier releases:
  .whisper-failed, .whisper-polluted, .annotate-failed, .pipeline-crashed

`any_failure_sidecar_with_kind` recognises both new and legacy formats
so nothing gets stranded. Only the new `.pipeline-failed` is written
going forward; legacy sidecars get cleaned by the user's next retry.

Filename, not extension, is the state signal — sidecars are
extension-less so Jellyfin / Infuse never try to load them as
subtitles.
"""
from pathlib import Path
from typing import Optional

_PIPELINE_FAILED_SUFFIX = ".pipeline-failed"

_LEGACY_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".whisper-failed", "whisper failed"),
    (".whisper-polluted", "whisper polluted"),
    (".annotate-failed", "annotate failed"),
    (".pipeline-crashed", "pipeline crashed"),
)


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
    """Every sidecar path (new + legacy) that could indicate this video
    has failed. Used by retry / cleanup — delete everything that
    matches so a fresh pipeline attempt is un-blocked."""
    return [
        pipeline_failed_path(video),
        *(video.with_suffix(s) for s, _ in _LEGACY_SUFFIXES),
    ]


def any_failure_sidecar_with_kind(
    video: Path,
) -> Optional[tuple[str, str]]:
    """Return `(kind, reason)` from any failure sidecar on this video,
    or None if the video is not in a failed state.

    New-format `.pipeline-failed`: body is `<kind>: <reason>` — parsed.
    Legacy sidecars: kind derived from filename suffix, body is the
    reason. Returns the first match (new format preferred)."""
    p = pipeline_failed_path(video)
    if p.is_file():
        raw = _read_or_empty(p)
        if ":" in raw:
            kind, _, reason = raw.partition(":")
            return kind.strip(), reason.strip()
        return "failed", raw
    for suffix, kind in _LEGACY_SUFFIXES:
        legacy = video.with_suffix(suffix)
        if legacy.is_file():
            return kind, _read_or_empty(legacy)
    return None


def any_failure_reason(video: Path) -> Optional[str]:
    """Convenience: just the reason string (without kind prefix), or
    None if the video isn't in a failed state. Retains the kind by
    joining as `<kind>: <reason>` for display."""
    got = any_failure_sidecar_with_kind(video)
    if got is None:
        return None
    kind, reason = got
    return f"{kind}: {reason}" if reason else kind


def _read_or_empty(sidecar: Path) -> str:
    try:
        return sidecar.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
