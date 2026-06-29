"""SRT annotation — Claude (Sonnet) flags U.S.-culture-specific references and
embeds short 繁體中文 notes into each cue's text.

Pure function entry point: `annotate_srt(srt_path) -> str` reads an SRT,
runs the Sonnet annotation pass, returns the annotated SRT text. Callers
(tasks.py for both bt and yt paths) are responsible for writing the result
to canonical, handling failures, and updating job status.

No `※ annotated` sentinel cue is inserted — under the file-existence state
model, the canonical SRT existing IS the "annotated" signal, so an in-body
marker would be redundant overlay noise.
"""
import re
from typing import Callable, Optional
from pathlib import Path

from claude_client import generate_json

# Cap per-call cue count so output JSON stays well under the model's
# output limit.
CHUNK_SIZE = 800

ANNOTATION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "cue": {"type": "integer"},
            "entity": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["cue", "entity", "note"],
    },
}

PROMPT_TEMPLATE = """\
You annotate English transcripts for a Taiwanese viewer with solid everyday \
English. The viewer can follow conversation, but misses culturally specific \
references particular to U.S. (or other domestic-audience) media, sports, \
politics, business, regional life, etc. The transcript may be on any topic.

For each cue containing such a reference, output a short 繁體中文 note. \
Most cues will need no annotation — be selective.

ANNOTATE references to:
- Named people whose identity is needed to follow the line (athletes, hosts, \
politicians, business figures, creators, niche celebrities, retired figures).
- Specific places, neighborhoods, schools, regional towns a Taiwanese viewer \
wouldn't recognize on sight.
- Domain brands tied to local daily life (regional restaurants, retailers, \
healthcare/insurance terms, local services).
- Domain jargon the speaker assumes the listener knows: sports rules and \
gameplay terms, political process terms, finance terms, tech-scene terms.
- Slang, regional expressions, in-jokes, memes.
- TV / film / music references that assume a domestic audience.
- Idioms and phrasal verbs whose figurative meaning isn't obvious from the \
literal words — even common ones (e.g. "didn't say jack" = didn't say anything; \
"put a dent in" = make a noticeable improvement; "the bomb" = excellent; \
"break into" = illegally enter). An intermediate ESL viewer with solid everyday \
English may still miss these. When in doubt, annotate.

DO NOT ANNOTATE — these are already known to the audience:
- Globally famous companies (Apple, Google, Microsoft, Amazon, Netflix, \
Tesla, Coca-Cola, McDonald's, Starbucks, IKEA).
- Globally famous people (LeBron James, Taylor Swift, Elon Musk, Obama, Trump, \
Messi, Ronaldo).
- Major U.S. cities (New York, Los Angeles, San Francisco, Chicago, Boston, Miami).
- Major TV networks (ABC, NBC, CBS, FOX, CNN, BBC).
- Top-level sports entities: leagues (NBA, NFL, MLB, NHL, MLS), big trophies \
(Super Bowl, World Series, Stanley Cup), marquee teams (Lakers, Yankees, \
Cowboys, Knicks, Celtics).
- Standard dictionary words used in their plain literal sense.
- Anything obvious from earlier context within the same transcript.

OUTPUT EMPTY ARRAY `[]` when nothing in this chunk warrants annotation. \
Common cases:
- The transcript is in a language other than English (Chinese podcast, \
Japanese drama, etc.) — return [] for the whole chunk; don't try to map \
foreign-language references.
- The chunk is mostly music, sound effects, ambient noise, or non-verbal \
content like `(DRUM BEATING)` / `(LAUGHTER)`.
- The dialogue is fully self-contained — no named entities, jargon, or \
idioms an ESL viewer would miss. A normal everyday conversation about \
nothing specific is a frequent case; don't manufacture annotations.

When in doubt about whether something warrants annotation, prefer skipping. \
Sparse annotation is correct; over-annotation pollutes the viewing experience.

RULES for each note:
- 繁體中文, under 40 characters.
- The note MUST add substance beyond decoding a name. Forbidden notes: \
「X是現役球員」, 「Y是電視主持人」, 「Z是城市」. \
Required: include role/team/era/why-they-matter so the viewer instantly gets \
what the speaker is alluding to. Example good: 「Patrick Ewing: 90 年代尼克 \
中鋒名宿，多年未能拿冠軍的代表人物」.
- Transcription errors: if a name is clearly Whisper-mishearing a real \
person, write the note as if the correct name was used — don't acknowledge \
the typo. If you can't confidently identify the intended reference, skip the cue.
- One annotation per entity for the whole transcript. Don't re-annotate \
anyone who appears in the "already annotated" list below (even if the cue \
spells them differently — match by intent).

OUTPUT: JSON array of {"cue": <int>, "entity": <str>, "note": <str>}.
- 'entity' = short canonical key for the thing being explained \
(lowercase, ascii, underscores; e.g. "wemby", "patrick_ewing", "tri_state_area", \
"flagrant_foul"). Use the SAME key for the same person/concept across the \
transcript so dedup works.

Already annotated entities (skip — do not re-annotate these): %s

SRT chunk:
%s
"""


# ── SRT parsing ────────────────────────────────────────────────────────────

_BLOCK_SEP = re.compile(r"\n\s*\n")


def parse_srt(text: str) -> list[dict]:
    """Return list of {idx, time, lines} where lines is the list of text lines."""
    out = []
    for block in _BLOCK_SEP.split(text.strip()):
        block = block.strip()
        if not block:
            continue
        rows = block.split("\n")
        if len(rows) < 3:
            continue
        try:
            idx = int(rows[0].strip())
        except ValueError:
            continue
        time_line = rows[1].strip()
        text_lines = rows[2:]
        out.append({"idx": idx, "time": time_line, "lines": text_lines})
    return out


def render_srt(cues: list[dict]) -> str:
    parts = []
    for c in cues:
        parts.append(f"{c['idx']}\n{c['time']}\n" + "\n".join(c["lines"]))
    return "\n\n".join(parts) + "\n"


def render_chunk_for_prompt(chunk: list[dict]) -> str:
    """Compact representation sent to the LLM — just idx + plain text per cue.

    Strips timestamps to save tokens; the model doesn't need them and they
    can throw off cue-index matching.
    """
    parts = []
    for c in chunk:
        text = " ".join(line.strip() for line in c["lines"]).strip()
        parts.append(f"{c['idx']}: {text}")
    return "\n".join(parts)


def _is_sentinel_cue(cue: dict) -> bool:
    """A cue whose visible text is just our ※ markers. Phase-2 pipelines
    don't emit these any more, but legacy annotated SRTs (pre-Phase 2) may
    still carry `※ source: …` / `※ annotated` / `※ os failed: …` cues,
    so we still filter them out of LLM chunks so Claude doesn't re-annotate
    them. Re-render preserves them verbatim."""
    visible = [ln.strip() for ln in cue.get("lines", []) if ln.strip()]
    return bool(visible) and all(ln.startswith("※") for ln in visible)


# ── Annotation entry point ────────────────────────────────────────────────

def annotate_srt(
    srt_path: Path,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Annotate the SRT at `srt_path`; return the annotated SRT text.

    Returns None if `is_cancelled()` ever returns True between chunks
    (caller decides what to do — typically: stop without writing canonical).

    Raises on any other failure (missing file, no parseable cues, API
    error). Callers should catch and write `<stem>.annotate-failed`
    sidecar so the scan loop stops retrying.
    """
    if not srt_path.exists():
        raise RuntimeError(f"SRT file missing on disk: {srt_path}")

    try:
        raw = srt_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Western-release SRTs are often cp1252 / Latin-1. Latin-1 single-byte
        # decode never raises; result gets re-saved as UTF-8 below.
        raw = srt_path.read_text(encoding="latin-1")
    cues = parse_srt(raw)
    if not cues:
        raise RuntimeError("SRT contained no parseable cues")

    # Skip any legacy ※-prefix cues when chunking for the LLM (re-render
    # still includes them so old annotated SRTs that get re-processed
    # don't lose anything).
    annotatable_cues = [c for c in cues if not _is_sentinel_cue(c)]
    if not annotatable_cues:
        raise RuntimeError("SRT had only sentinel cues, no dialogue to annotate")

    notes: dict[int, str] = {}
    seen_entities: set[str] = set()

    for start in range(0, len(annotatable_cues), CHUNK_SIZE):
        if is_cancelled is not None and is_cancelled():
            return None

        chunk = annotatable_cues[start:start + CHUNK_SIZE]
        chunk_text = render_chunk_for_prompt(chunk)
        already = ", ".join(sorted(seen_entities)) if seen_entities else "(none yet)"
        prompt = PROMPT_TEMPLATE % (already, chunk_text)
        result = generate_json(prompt, ANNOTATION_SCHEMA, temperature=0.2)
        for entry in result:
            try:
                cue_idx = int(entry["cue"])
                entity = str(entry.get("entity", "")).strip().lower()
                note = str(entry["note"]).strip()
            except (KeyError, ValueError, TypeError):
                continue
            if not note:
                continue
            # Hard dedup belt-and-suspenders for when the model ignores
            # the "already annotated" list.
            if entity and entity in seen_entities:
                continue
            notes[cue_idx] = note
            if entity:
                seen_entities.add(entity)

    for c in cues:
        note = notes.get(c["idx"])
        if note:
            c["lines"].append(f"※ {note}")

    return render_srt(cues)
