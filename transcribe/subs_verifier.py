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
local video file. Candidates come from either hash search (mis-tagged uploads \
exist — the hash points to a sub for a different work) or text search (search \
hits may include the wrong season/episode or unrelated titles that share a \
word). Your job is to reject the wrong ones.

Local video filename:
{video_name}

Candidates:
{candidate_list}

Per candidate fields:
- release: uploader's subtitle filename (often a release name with noise \
tokens like resolution, codec, group)
- title: canonical title from OpenSubtitles' metadata. For TV this is the \
EPISODE name (e.g. "Pilot"), NOT the show name — look at `show` for that.
- year: release year (movie) or first-air year (TV)
- show: parent series title (only present for TV episodes)
- season/episode: SxxExx (only present for TV episodes)

Decide which candidate (if any) is the same work as the local video.

For TV: the local filename usually contains `SxxExx`. The candidate matches \
only if its show + season + episode all align. A candidate with the right \
show but wrong S/E is NOT a match.

For movies: title and year should both align (allowing for translated/local \
titles, noise tokens, and punctuation differences).

Be lenient across language differences, transliterations, abbreviations, \
punctuation, and noise tokens. Be strict about clearly different works \
(e.g. "Spider-Man: Far from Home" vs "Whiplash" local video) and wrong \
episodes (e.g. S01E01 candidate vs S01E05 local).

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
        parent = details.get("parent_title")
        season = details.get("season_number")
        episode = details.get("episode_number")

        block = [
            f"- id: {cand_id}",
            f"  release: {release}",
            f"  title: {title}",
            f"  year: {year}",
        ]
        if parent:
            block.append(f"  show: {parent}")
        if season is not None and episode is not None:
            block.append(f"  season/episode: S{int(season):02d}E{int(episode):02d}")
        lines.append("\n".join(block))

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
