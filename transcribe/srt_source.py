"""Sidecar files that record pipeline failure for a video.

Four failure modes the background scan loop needs to remember:

  <stem>.whisper-failed     — whisper run did not produce an SRT
  <stem>.whisper-polluted   — whisper produced an SRT but it's a
                              hallucination loop ("No. No. No." × 100s).
                              Pipeline detected the pattern, no candidate
                              was available to substitute, so it refused
                              to promote the junk to canonical.
  <stem>.annotate-failed    — annotation pass crashed (SRT still playable
                              at <stem>.srt; only the ※ annotation work
                              never landed)
  <stem>.pipeline-crashed   — catch-all for unexpected exceptions inside
                              process_bt_file / process_video that the
                              stage-specific try/except blocks didn't
                              catch. Without this sidecar the scan loop
                              would re-enqueue the file every 30s in a
                              retry-forever loop.

Body is the (short, single-line) error message so the user can see WHY
without opening docker logs. Filename, not file extension, is the
state signal — both sidecars are extension-less so Jellyfin / Infuse
never try to load them as subtitles.

The UI ↻ button clears both the sidecar and (for whisper-failed) any
matching SRT, so the next scan tick replays the pipeline fresh.

`※ annotated` is the only marker still living inside SRT bodies — it's
the natural in-place signal of "annotation has been applied to this
SRT file" and lives in annotate.py.
"""
from pathlib import Path


def _short(msg: str) -> str:
    """Collapse whitespace + cap so a multi-line traceback doesn't
    fill the sidecar with junk."""
    return msg.replace("\n", " ").replace("\r", " ").strip()[:500]


def whisper_failed_path(video: Path) -> Path:
    """Path of the whisper-failed sidecar for a given video."""
    return video.with_suffix(".whisper-failed")


def whisper_polluted_path(video: Path) -> Path:
    """Path of the whisper-polluted sidecar for a given video."""
    return video.with_suffix(".whisper-polluted")


def annotate_failed_path(video: Path) -> Path:
    """Path of the annotate-failed sidecar for a given video."""
    return video.with_suffix(".annotate-failed")


def pipeline_crashed_path(video: Path) -> Path:
    """Path of the pipeline-crashed sidecar for a given video (catch-all
    for unexpected exceptions surfaced via `_catch_unhandled`)."""
    return video.with_suffix(".pipeline-crashed")


def stamp_whisper_failed(video: Path, error: str) -> None:
    """Write a `<stem>.whisper-failed` sidecar so the scan loop stops
    retrying. Body is the short error reason."""
    p = whisper_failed_path(video)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_short(error) + "\n", encoding="utf-8")


def stamp_whisper_polluted(video: Path, reason: str) -> None:
    """Write a `<stem>.whisper-polluted` sidecar so the scan loop stops
    retrying. Body is the detected loop signature (e.g. `437 consecutive
    identical cues 'no.'`). User intervention is needed — dropping a
    bundled SRT into `/bt`, refetching OS, or accepting the limitation
    on this episode."""
    p = whisper_polluted_path(video)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_short(reason) + "\n", encoding="utf-8")


def stamp_annotate_failed(video: Path, error: str) -> None:
    """Write a `<stem>.annotate-failed` sidecar. The SRT at <stem>.srt
    is left untouched and remains usable for playback minus annotations."""
    p = annotate_failed_path(video)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_short(error) + "\n", encoding="utf-8")


def stamp_pipeline_crashed(video: Path, error: str) -> None:
    """Write a `<stem>.pipeline-crashed` sidecar so the scan loop stops
    re-enqueueing this file after an unhandled exception. Body is the
    short exception summary."""
    p = pipeline_crashed_path(video)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_short(error) + "\n", encoding="utf-8")


def read_failure_reason(sidecar: Path) -> str | None:
    """Return the failure reason recorded in a sidecar file, or None
    if it doesn't exist / can't be read."""
    if not sidecar.is_file():
        return None
    try:
        return sidecar.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
