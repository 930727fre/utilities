"""SRT annotation worker — calls Gemini to flag U.S.-culture-specific references
and embeds short 繁體中文 notes into each cue's text.
"""
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from claude_client import generate_json
from storage import get_job, upsert_job

DOWNLOADS_DIR = Path("/app/data/downloads")

# Single worker — annotation is API-bound, not time-critical. Keep predictable.
annotate_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotate-worker")

# Cap per-call cue count so output JSON stays well under flash-lite's output limit.
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
- Words in any standard English dictionary; ordinary English idioms.
- Anything obvious from earlier context within the same transcript.

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# ── Worker ────────────────────────────────────────────────────────────────

def annotate_job(job_id: str):
    try:
        _do_annotate(job_id)
    except Exception as exc:
        traceback.print_exc()
        job = get_job(job_id)
        if job and job["status"] == "ANNOTATING":
            job["status"] = "SUCCESS"
            job["annotation_error"] = f"Annotation failed: {exc}"
            job["updated_at"] = _now()
            upsert_job(job)


def _do_annotate(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] != "ANNOTATING":
        return

    srt_name = (job.get("files") or {}).get("srt")
    if not srt_name:
        raise RuntimeError("Job has no SRT file")
    srt_path = DOWNLOADS_DIR / srt_name
    if not srt_path.exists():
        raise RuntimeError(f"SRT file missing on disk: {srt_path}")

    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not cues:
        raise RuntimeError("SRT contained no parseable cues")

    notes: dict[int, str] = {}
    seen_entities: set[str] = set()

    for start in range(0, len(cues), CHUNK_SIZE):
        # Check for cancellation between chunks — keeps annotation responsive
        # to a Delete (frontend disables it, but other callers can DELETE).
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            return

        chunk = cues[start:start + CHUNK_SIZE]
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
            # Hard dedup belt-and-suspenders for when the model ignores the
            # "already annotated" list (which it sometimes does).
            if entity and entity in seen_entities:
                continue
            notes[cue_idx] = note
            if entity:
                seen_entities.add(entity)

    # Apply
    for c in cues:
        note = notes.get(c["idx"])
        if note:
            c["lines"].append(f"※ {note}")

    # Re-check before writing
    current = get_job(job_id)
    if not current or current["status"] == "DELETED":
        return

    # Write to a sibling .annotated.srt, leave the original untouched so the
    # caller can still get a clean transcript for VLC / re-annotation.
    annotated_name = srt_path.stem + ".annotated.srt"
    annotated_path = srt_path.with_name(annotated_name)
    annotated_path.write_text(render_srt(cues), encoding="utf-8")

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return
    job["status"] = "SUCCESS"
    job["annotated"] = True
    job["annotation_error"] = None
    files = job.get("files") or {}
    files["srt_annotated"] = annotated_name
    job["files"] = files
    job["updated_at"] = _now()
    upsert_job(job)
