"""Append a far-future sentinel cue tagging which pipeline step produced an SRT.

Lives at 99:59:58 — past any plausible video runtime, so it never displays.
Parallel to the `※ annotated` sentinel appended by annotate.py at 99:59:59.

annotate.py's parse → re-render preserves this cue automatically; the
annotated SRT ends up carrying both markers.
"""
from pathlib import Path


def stamp_source(srt_path: Path, source: str) -> None:
    with open(srt_path, "a", encoding="utf-8") as f:
        f.write(
            f"\n\n99998\n"
            f"99:59:58,998 --> 99:59:58,999\n"
            f"※ source: {source}\n"
        )
