"""Two-stage subtitle verification.

`verify_candidate(video, candidates)` — metadata-only prefilter for an
OpenSubtitles result list. Asks Haiku whether each candidate's release /
title / season-episode metadata matches the local filename, picks the
best one. Cheap, runs BEFORE we burn an OS download quota on the wrong
file.

`verify_against_whisper(whisper_srt, candidate_srt)` — content-level
gate. Whisper output is the ground-truth listening reference; the
candidate is a "literary upgrade" we accept only if its actual cue text
+ timing demonstrate it's subtitling the same audio.

Why two? Cheap-first ordering: metadata filter discards obviously wrong
candidates (different show, wrong episode) before we even download, then
content gate catches the failures metadata can't see — wrong cut (right
movie but Snyder Cut vs theatrical), forced subs masquerading as full
dialogue, sub mis-labelled as English. Both layers are needed; neither
subsumes the other.
"""
import os
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from annotate import parse_srt
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


# ── Content-level verification against whisper ────────────────────────────

# Density gate: candidate's cue count vs whisper's. Forced subs typically
# have ~10 cues spanning a 50-min episode, vs ~500 for whisper full dialogue.
# Ratio guards against both directions (SDH candidates may have ~1.2x
# whisper's cue count, still healthy).
_DENSITY_MIN_RATIO = 0.4

# Time-aligned fuzzy match parameters.
_SAMPLE_CUES = 20             # number of whisper cues sampled evenly across the file
_TIME_WINDOW_S = 15.0         # ±window for finding candidate cues near a whisper cue's timestamp
_FUZZ_MIN_SCORE = 50          # rapidfuzz token_set_ratio threshold for "this pair matches"
_MATCH_RATIO_PASS = 0.5       # >= 50% of sampled whisper cues must find a fuzzy match


def _start_seconds(cue: dict) -> float:
    """Parse cue's start timestamp (HH:MM:SS,mmm) into seconds. Permissive
    on malformed: returns 0.0 so a bad cue sorts to the start of the
    timeline rather than aborting verification."""
    try:
        start = cue["time"].split(" -->")[0]
        h, m, rest = start.split(":")
        sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
    except (KeyError, ValueError, AttributeError):
        return 0.0


def _cue_text(cue: dict) -> str:
    """Flatten cue's text lines into a single lower-cased string for fuzzing."""
    return " ".join(ln.strip() for ln in cue.get("lines", []) if ln.strip()).lower()


def _real_cues(srt_path: Path) -> list[dict]:
    """Parse SRT and drop our ※ sentinel cues (idx >= 99000 by convention).
    Verification compares dialogue, not status overlay."""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cues = parse_srt(text)
    return [c for c in cues if c.get("idx", 0) < 99000]


def verify_against_whisper(
    whisper_srt: Path,
    candidate_srt: Path,
) -> tuple[bool, str]:
    """Pure-Python content gate. Returns (pass, reason).

    Stage 1 — density check: reject if candidate has dramatically fewer
    or more cues than whisper (catches forced subs / wrong content).

    Stage 2 — time-aligned fuzzy match: sample whisper cues across the
    file, find any candidate cue within ±15s of each sample, score
    token-set similarity. Pass if >= 50% of samples find a match scoring
    >= 50.

    rapidfuzz.token_set_ratio is robust to whisper mis-hearings,
    punctuation/casing differences, SDH descriptive cues, slight reorderings —
    everything that a strict character-level compare would falsely reject.
    """
    w_cues = _real_cues(whisper_srt)
    c_cues = _real_cues(candidate_srt)

    if not w_cues:
        return False, "whisper SRT empty or unparseable"
    if not c_cues:
        return False, "candidate SRT empty or unparseable"

    # Stage 1 — density
    density = min(len(w_cues), len(c_cues)) / max(len(w_cues), len(c_cues))
    if density < _DENSITY_MIN_RATIO:
        return False, (
            f"cue density {density:.2f} below {_DENSITY_MIN_RATIO} "
            f"(whisper={len(w_cues)}, candidate={len(c_cues)} — likely forced subs "
            f"or wrong content)"
        )

    # Stage 2 — time-aligned fuzzy match
    n_samples = min(_SAMPLE_CUES, len(w_cues))
    step = max(1, len(w_cues) // n_samples)
    samples = w_cues[::step][:n_samples]

    matches = 0
    for w_cue in samples:
        w_t = _start_seconds(w_cue)
        w_text = _cue_text(w_cue)
        if not w_text:
            continue

        nearby = [
            c for c in c_cues
            if abs(_start_seconds(c) - w_t) <= _TIME_WINDOW_S
        ]
        if not nearby:
            continue

        best_score = max(
            fuzz.token_set_ratio(w_text, _cue_text(c)) for c in nearby
        )
        if best_score >= _FUZZ_MIN_SCORE:
            matches += 1

    match_ratio = matches / n_samples if n_samples else 0.0
    if match_ratio >= _MATCH_RATIO_PASS:
        return True, (
            f"{matches}/{n_samples} sampled cues matched "
            f"(density {density:.2f})"
        )
    return False, (
        f"only {matches}/{n_samples} sampled cues matched, need "
        f"{int(_MATCH_RATIO_PASS * n_samples)} (density {density:.2f}) — "
        f"likely different content / wrong cut / wrong language"
    )
