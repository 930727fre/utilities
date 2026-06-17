"""LLM-assisted SRT-to-video matching.

Strict same-stem match misses bundled SRTs with language codes, release-group
tags, or odd naming. When the background loop is about to queue whisper for a
video that *might* already have a bundled subtitle, we ask Claude Haiku to look
at the folder listing and pick one. If matched, the caller renames the SRT to
match the video stem — restoring strict-match consistency for future scans and
Jellyfin.
"""
from pathlib import Path
from typing import Optional

from claude_client import generate_json

# Haiku is plenty smart for stem-matching and an order of magnitude cheaper
# than Sonnet. Override via ANTHROPIC_MATCH_MODEL env var if you really want.
import os
_MATCH_MODEL = os.environ.get("ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5-20251001")

_SCHEMA = {
    "type": "object",
    "properties": {"match": {"type": ["string", "null"]}},
    "required": ["match"],
}

_PROMPT_TEMPLATE = """\
You match a video file to its subtitle (.srt) sidecar in the same folder.

Video filename:
{video_name}

Candidate .srt files in the same folder:
{srt_list}

If one of the .srt files is the subtitle track for this video — same show + \
episode (or same movie title), ignoring differences in language code suffix \
(`.en.srt`, `.eng.srt`), release-group tags, resolution tags, version numbers, \
or whitespace/punctuation variations — respond with that filename verbatim.

If none of them corresponds, respond with null. Be strict: only match when \
you're confident it's the same video.

Output JSON: {{"match": "<exact filename from the list>" | null}}
"""


def find_matching_srt(video: Path) -> Optional[Path]:
    """Return the sibling .srt that LLM identifies as this video's subtitle, or None.

    Returns None on: no candidate .srt files, LLM saying no, LLM hallucinating
    a filename not in the listing, or API failure. Caller falls through to
    whisper in any of those cases.
    """
    folder = video.parent
    try:
        siblings = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".srt"
        )
    except OSError:
        return None
    if not siblings:
        return None

    sibling_names = [s.name for s in siblings]
    prompt = _PROMPT_TEMPLATE.format(
        video_name=video.name,
        srt_list="\n".join(f"- {n}" for n in sibling_names),
    )

    try:
        result = generate_json(
            prompt, _SCHEMA,
            model=_MATCH_MODEL,
            temperature=0.0,
            max_tokens=200,
        )
    except Exception as e:
        print(f"[srt-matcher] API error for {video.name!r}: {e}", flush=True)
        return None

    matched_name = result.get("match")
    if not matched_name:
        return None

    # Anti-hallucination: must be a real entry in the listing we showed it.
    if matched_name not in sibling_names:
        print(f"[srt-matcher] LLM returned {matched_name!r}, not in folder; ignoring", flush=True)
        return None

    return folder / matched_name
