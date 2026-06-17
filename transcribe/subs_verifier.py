"""LLM-assisted OpenSubtitles candidate verification.

`moviehash_match=True` only means the uploader claimed their sub corresponds
to this hash — there's no server-side check. Real-world bad data exists (e.g.
Spider-Man subs mis-tagged with Whiplash's hash). We need a second line that
reasons about whether the candidate actually refers to the same work.

Both signals available per candidate help here:
- `attributes.release` — the uploader's subtitle filename, usually a release
  string like `Movie.Title.YEAR.QUALITY.GROUP.mkv`
- `attributes.feature_details.title` / `.year` — canonical metadata from
  OpenSubtitles' film database

Haiku is cheap and reliably handles transliteration / abbreviation / noise
tokens / language variants when comparing these to the local filename.
"""
import os
from pathlib import Path
from typing import Optional

from claude_client import generate_json

_MATCH_MODEL = os.environ.get("ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5-20251001")

_SCHEMA = {
    "type": "object",
    "properties": {"match_id": {"type": ["string", "null"]}},
    "required": ["match_id"],
}

_PROMPT_TEMPLATE = """\
You verify that an OpenSubtitles subtitle candidate actually corresponds to a \
local video file. Mis-tagged uploads exist (the hash points to a sub for a \
different work) — your job is to catch them.

Local video filename:
{video_name}

Candidates (all share the file hash with the local video, but only the right \
one is the same movie/show):
{candidate_list}

Per candidate you see:
- release: the uploader's subtitle filename (often a release name with noise \
tokens like resolution, codec, group)
- title: canonical movie/show title from OpenSubtitles' metadata
- year: release year (movie) or first-air year (TV)

Decide which candidate (if any) is the same movie/show as the local video. \
Be lenient across language differences, transliterations, abbreviations, \
punctuation, and noise tokens. Be strict about clearly different works — \
e.g. "Spider-Man: Far from Home" candidate vs "Whiplash" local video is a \
mismatch even if the hash claims otherwise.

Respond with the matching candidate's id, or null if none match.

Output JSON: {{"match_id": "<id from list>" | null}}
"""


def verify_candidate(video: Path, candidates: list[dict]) -> Optional[dict]:
    """Pick the candidate Haiku confirms matches this video, or None.

    `candidates` are raw OpenSubtitles search result items (each has an
    `attributes` dict). Returns the chosen item from the input list, or
    None if Haiku rejects all of them / API fails.
    """
    if not candidates:
        return None

    id_map: dict[str, dict] = {}
    lines: list[str] = []
    for i, cand in enumerate(candidates):
        cand_id = f"c{i}"
        id_map[cand_id] = cand
        attrs = cand.get("attributes") or {}
        details = attrs.get("feature_details") or {}
        release = attrs.get("release") or "?"
        title = details.get("title") or "?"
        year = details.get("year") or "?"
        lines.append(
            f"- id: {cand_id}\n  release: {release}\n  title: {title}\n  year: {year}"
        )

    prompt = _PROMPT_TEMPLATE.format(
        video_name=video.name,
        candidate_list="\n".join(lines),
    )

    try:
        result = generate_json(
            prompt, _SCHEMA,
            model=_MATCH_MODEL,
            temperature=0.0,
            max_tokens=200,
        )
    except Exception as e:
        print(f"[subs-verify] API error for {video.name!r}: {e}", flush=True)
        return None

    match_id = result.get("match_id")
    if not match_id:
        print(f"[subs-verify] no candidate matched {video.name!r} "
              f"(considered {len(candidates)})", flush=True)
        return None
    if match_id not in id_map:
        print(f"[subs-verify] LLM returned {match_id!r}, not a valid id; ignoring", flush=True)
        return None
    return id_map[match_id]
