"""Two-stage subtitle verification.

`verify_candidate(video, candidates)` — metadata-only prefilter for an
OpenSubtitles result list. Asks Haiku whether each candidate's release /
title / season-episode metadata matches the local filename, picks the
best one. Cheap, runs BEFORE we burn an OS download quota on the wrong
file.

`verify_against_whisper(whisper_srt, candidate_srt)` — content-level
gate via word error rate (`jiwer`). Whisper output is the ground-truth
listening reference; the candidate is a "literary upgrade" we accept
only if its full transcript text is close enough to whisper's that
they're plausibly the same audio. Timing is intentionally ignored —
ffsubsync handles alignment downstream once a candidate passes.

Why two? Cheap-first ordering: metadata filter discards obviously wrong
candidates (different show, wrong episode) before we even download, then
WER gate catches the failures metadata can't see — wrong cut (right
movie but Snyder Cut vs theatrical), forced subs masquerading as full
dialogue, sub mis-labelled as English. Both layers are needed; neither
subsumes the other.
"""
import os
import re
from pathlib import Path
from typing import Optional

import jiwer

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


# ── Content-level verification against whisper (WER) ──────────────────────

# Density gate: cheap pre-filter for forced subs / pathologically
# mismatched lengths. WER would also reject these but density check is
# O(1) vs WER's O(n*m). Symmetric so a candidate that's MUCH longer than
# whisper (rare; over-eager SDH) gets rejected too.
_DENSITY_MIN_RATIO = 0.4

# WER threshold. Calibration from the whisper / ASR-eval literature:
# clean human transcript vs whisper-large output on the same audio
# usually scores 0.1–0.3. With the extra noise of release-mismatch (CC
# vs full dialogue, occasional missing lines, etc.) realistic same-
# content WER lands around 0.2–0.5. Different content / wrong cut /
# wrong language climbs >0.7. 0.5 is the conventional pass cutoff and
# leaves safety margin in both directions.
_WER_PASS_MAX = 0.5

# Strip SRT formatting tags before normalization — `<i>...</i>`,
# `<b>`, `{\an8}` positioning markers, etc. — so the WER score reflects
# spoken-word content only.
_SRT_TAG_RE = re.compile(r"<[^>]+>|\{[^}]+\}")

# Drop everything that isn't a word character, whitespace, or apostrophe
# (apostrophes are kept so "don't" / "it's" don't get split mid-word).
_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_MULTI_WS_RE = re.compile(r"\s+")


def _cue_text(cue: dict) -> str:
    """Flatten a cue's lines into a single string."""
    return " ".join(ln.strip() for ln in cue.get("lines", []) if ln.strip())


def _real_cues(srt_path: Path) -> list[dict]:
    """Parse SRT and drop our ※ sentinel cues (idx >= 99000 by convention).
    Verification compares dialogue, not status overlay."""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cues = parse_srt(text)
    return [c for c in cues if c.get("idx", 0) < 99000]


def _normalized_full_text(cues: list[dict]) -> str:
    """Concatenate every cue's text, strip SRT formatting, lowercase,
    drop punctuation (except apostrophes), collapse whitespace. Result is
    a clean space-separated word stream ready to feed jiwer.wer."""
    raw = " ".join(_cue_text(c) for c in cues)
    s = _SRT_TAG_RE.sub("", raw)
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _MULTI_WS_RE.sub(" ", s)
    return s.strip()


def verify_against_whisper(
    whisper_srt: Path,
    candidate_srt: Path,
) -> tuple[bool, str]:
    """WER-based content gate. Returns (pass, reason).

    Stage 1 — density check: reject if candidate's cue count is less
    than 40% (or more than 250%) of whisper's. Catches forced subs and
    wildly mismatched lengths without paying the WER's quadratic cost.

    Stage 2 — WER: concat all cue text from each side, strip SRT
    formatting + punctuation, lowercase. Compute word error rate
    between whisper (reference) and candidate (hypothesis); pass if
    WER ≤ 0.5. jiwer is the canonical ASR-eval library used across the
    whisper / Common Voice / etc. ecosystem, so the threshold has
    well-known calibration.

    Timing is deliberately not considered — ffsubsync handles alignment
    after this gate passes. Verify's only job is "is this the same
    transcript content."
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

    # Stage 2 — WER
    w_text = _normalized_full_text(w_cues)
    c_text = _normalized_full_text(c_cues)
    if not w_text or not c_text:
        return False, "normalized transcript empty after cleanup"

    try:
        wer = jiwer.wer(w_text, c_text)
    except ValueError as e:
        return False, f"WER computation failed: {e}"

    if wer <= _WER_PASS_MAX:
        return True, f"WER {wer:.2f} ≤ {_WER_PASS_MAX} (density {density:.2f})"
    return False, (
        f"WER {wer:.2f} > {_WER_PASS_MAX} (density {density:.2f}) — "
        f"likely different content / wrong cut / wrong language"
    )
