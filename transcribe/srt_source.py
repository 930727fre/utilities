"""Far-future sentinel cues that double as on-disk state markers for the
qb annotation pipeline.

Every sentinel lives at 99:59:5x — past any plausible video runtime, so a
playing user never sees them, but `grep ※` on the SRT recovers everything
the loop needs to decide what's been tried, what succeeded, and what
failed.

Markers in use:
  ※ source: whisper / opensubtitles-hash / opensubtitles-text
  ※ annotated
  ※ whisper failed: <reason>          (NEW — stops auto-retry)
  ※ annotate failed: <reason>          (NEW — stops auto-retry)

annotate.py's parse → re-render preserves these cues automatically, so
the annotated SRT ends up carrying all relevant markers.
"""
from pathlib import Path

WHISPER_FAILED_MARKER = "※ whisper failed:"
ANNOTATE_FAILED_MARKER = "※ annotate failed:"


def _short(msg: str) -> str:
    # Sentinel is one SRT cue, so collapse newlines and cap length so a
    # multi-line traceback doesn't explode the file.
    return msg.replace("\n", " ").replace("\r", " ").strip()[:200]


def stamp_source(srt_path: Path, source: str) -> None:
    with open(srt_path, "a", encoding="utf-8") as f:
        f.write(
            f"\n\n99998\n"
            f"99:59:58,998 --> 99:59:58,999\n"
            f"※ source: {source}\n"
        )


def stamp_whisper_failed(srt_path: Path, error: str) -> None:
    """Write a single-cue SRT recording that whisper was tried and failed.

    Used in place of the regular whisper output so the scan loop sees an
    SRT and knows not to retry — without committing a usable transcript.
    """
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(
        f"99997\n"
        f"99:59:57,998 --> 99:59:57,999\n"
        f"{WHISPER_FAILED_MARKER} {_short(error)}\n",
        encoding="utf-8",
    )


def stamp_annotate_failed(srt_path: Path, error: str) -> None:
    """Append an annotation-failed sentinel to an existing SRT.

    The transcript stays intact and usable; the marker tells the loop not
    to keep re-firing the Claude call on every tick.
    """
    with open(srt_path, "a", encoding="utf-8") as f:
        f.write(
            f"\n\n99996\n"
            f"99:59:56,998 --> 99:59:56,999\n"
            f"{ANNOTATE_FAILED_MARKER} {_short(error)}\n"
        )
