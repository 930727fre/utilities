"""Sentinel cues that double as on-disk state markers and a short
status overlay the user sees at video start.

Sentinels sit in the 00:00:00 → 00:00:05 window — most shows' first real
dialogue cue starts at 5s+ (intro, cold open card, etc.), so the markers
flash for a couple of seconds at playback start and then disappear. The
user gets immediate confirmation of "which pipeline produced this SRT,
did annotation run, did anything fail" without opening the UI. A bunched
cold open with dialogue at 0:01 will see a tiny overlap with the
sentinel — acceptable given the diagnostic value.

annotate.py's parse → re-render preserves these cues automatically, so
the annotated SRT ends up carrying all relevant markers.

Markers in use (each in its own time window so a success run shows
source-then-annotated in sequence rather than overlapping):
  00:00:00 → 00:00:02   ※ source: whisper / opensubtitles-hash / opensubtitles-text
  00:00:02 → 00:00:04   ※ annotated
  00:00:00 → 00:00:03   ※ whisper failed: <reason>     (sole cue; whisper paths only)
  00:00:02 → 00:00:05   ※ annotate failed: <reason>    (appended after source)

Cue indices live in the 99996+ band so they can never collide with real
content cue numbers (longest movie SRTs are ~5000 cues).
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
            f"00:00:00,000 --> 00:00:02,000\n"
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
        f"00:00:00,000 --> 00:00:03,000\n"
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
            f"00:00:02,000 --> 00:00:05,000\n"
            f"{ANNOTATE_FAILED_MARKER} {_short(error)}\n"
        )
