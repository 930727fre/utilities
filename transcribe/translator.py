"""English-to-zh-TW SRT translator — Claude Haiku drives the bt
"translate to 中" button. Reuses annotate.py's SRT parse → chunk →
forced-JSON → render pattern. Translation is a well-trodden,
low-creativity task; Haiku's instruction-following is stronger than
Haiku at the structural constraint we need (every input
cue must come back as an entry, no dropping short interjections that
shift later cues onto wrong timestamps).

Output SRT is structurally identical to the input — same cue indices,
same timestamps — just the dialogue lines replaced with 繁體中文 and the
existing sentinel cues (`※ source: …`, `※ annotated`, etc.) carried
through unchanged. A new `※ source: llm-translated` sentinel gets
appended at the file end with timestamp 00:00:00. Players honor
timestamp order, so the source tag flashes at playback start same as
every other source.
"""
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from annotate import parse_srt, render_srt  # reuse the same parser/renderer
from claude_client import generate_json
from srt_source import stamp_source

ZH_SUFFIX = ".zh-tw.srt"

# Two workers — Haiku is API-bound; running two episodes in parallel
# halves season translation time without straining Anthropic Tier 2's
# Haiku TPM/RPM budget. (Per-episode chunks still go serial within one
# worker, so cue ordering inside any single SRT stays deterministic.)
translator_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="translator-worker")

# Cue count per call. Originally 400 cues — Flash Lite's output token cap
# wasn't the issue, but at that size the model would silently drop short
# cues ("Yeah.", "Asshole!", interjections) and renumber the rest,
# causing the visible-on-screen translation to slide off the spoken
# dialogue by N cues even though every timestamp matched verbatim.
# 200 keeps each call's accounting tight without doubling wall-clock
# noticeably (Flash Lite returns in ~5-10s either way).
CHUNK_SIZE = 200

_MODEL = os.environ.get("TRANSLATE_MODEL", "claude-haiku-4-5-20251001")

_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "cue": {"type": "integer"},
            "lines": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["cue", "lines"],
    },
}

_PROMPT_TEMPLATE = """\
Translate the following English SRT cues to 繁體中文 (Taiwan).

CRITICAL — cue alignment integrity:
- The input has %d cues. The output JSON array MUST contain EXACTLY %d \
entries — one per input cue, in input order.
- DO NOT drop, merge, skip, or combine cues — not even very short ones \
("Yeah.", "Asshole!", "Mm-hmm.", single interjections, repeated lines). \
A subtitle viewer relies on every cue having a translation at its own \
timestamp; missing entries shift every later cue's content onto the wrong \
timestamp and break the entire viewing experience.
- Use the EXACT integer `cue` index shown in the input for each output \
entry. Do not renumber, increment, or invent new indices.
- If a cue's content is genuinely untranslatable (e.g. pure music notation, \
already in Chinese), still emit an entry — copy the original text or use \
an empty string for `lines`, but emit the entry.

Translation rules:
- Translate the DIALOGUE faithfully and naturally in Taiwanese idiom; do not \
add explanations or expand.
- Preserve the line break structure within each cue: a cue with 2 input lines \
must come back with 2 output lines.
- SDH-style audio annotations like `[applause]`, `[music]`, `[door slams]` → \
translate concisely as 「[掌聲]」「[音樂]」「[關門聲]」 etc.
- Song lyrics (lines marked with `♪` or all-caps singing): translate the meaning \
naturally; keep `♪` markers if present in the source.
- Speaker labels (e.g. `MICHAEL:`, `-CARMELA:`): keep the label format but \
translate the dialogue part. Names stay in original Roman script.
- Personal names inside dialogue (Tony, Carmela, Christopher, Junior, \
Dr. Cusamano, Pussy, etc.): keep in the original Roman script verbatim — \
DO NOT transliterate into Chinese characters (write "Tony" not "東尼", \
"Carmela" not "卡梅拉"). This applies even if a name has a well-known \
Chinese rendering. Place names follow the same rule unless they are \
canonical 繁體中文 terms (e.g. "New York" stays "紐約" because that is \
the standard rendering, but "Newark" stays "Newark").
- Lines that already contain `※` (our own sentinel markers like \
`※ source: …`, `※ annotated`): pass through UNCHANGED, do not translate, \
do not edit.

OUTPUT JSON: array of {"cue": <int>, "lines": [<str>, ...]}. EXACTLY %d \
entries, one per input cue, in input order.

SRT chunk:
%s
"""


def _render_chunk_for_prompt(chunk: list[dict]) -> str:
    parts = []
    for c in chunk:
        body = "\n".join(c["lines"])
        parts.append(f"[cue {c['idx']}]\n{body}")
    return "\n\n".join(parts)


def translate_to_zh(src_srt: Path, out_path: Path) -> None:
    """Read `src_srt`, translate every cue's dialogue to 繁體中文, write the
    result to `out_path` (overwriting if it exists). Raises on any
    parser / API / write failure — caller stamps `.error` on exception.
    """
    try:
        raw = src_srt.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = src_srt.read_text(encoding="latin-1")

    cues = parse_srt(raw)
    if not cues:
        raise RuntimeError(f"source SRT had no parseable cues: {src_srt}")

    translations: dict[int, list[str]] = {}

    for start in range(0, len(cues), CHUNK_SIZE):
        chunk = cues[start:start + CHUNK_SIZE]
        chunk_text = _render_chunk_for_prompt(chunk)
        expected = len(chunk)
        prompt = _PROMPT_TEMPLATE % (expected, expected, expected, chunk_text)

        # Up to two attempts: if Haiku's first response drops cues (its
        # known failure mode is silently collapsing short interjections),
        # the indices of everything afterwards in that chunk shift onto
        # the wrong on-screen timestamp. Detect by count + by checking
        # which input cue indices got covered; retry once before giving
        # up and falling back to original English for the missing cues.
        valid: dict[int, list[str]] = {}
        chunk_idxs = {c["idx"] for c in chunk}
        for attempt in range(2):
            # max_tokens needs headroom for 200 translated cues × 2-3 short
            # lines each at ~30 tokens of CJK + JSON formatting per line.
            # 16k gives a comfortable ceiling; Haiku's hard cap is 16384.
            result = generate_json(prompt, _SCHEMA, temperature=0.0,
                                   model=_MODEL, max_tokens=16000)
            valid = {}
            for entry in result:
                try:
                    cue_idx = int(entry["cue"])
                    lines = [str(ln) for ln in entry["lines"]]
                except (KeyError, ValueError, TypeError):
                    continue
                if cue_idx not in chunk_idxs:
                    # Haiku hallucinated an index outside this chunk —
                    # could be the symptom of a shifted-renumbering pass.
                    # Reject so it can't poison a cue from another chunk.
                    continue
                valid[cue_idx] = lines
            missing = chunk_idxs - valid.keys()
            if not missing:
                break
            print(f"[translator] chunk {start}-{start+expected}: "
                  f"Haiku returned {len(valid)}/{expected} cues "
                  f"(missing {len(missing)}); attempt {attempt+1}/2", flush=True)
        else:
            # Both attempts dropped cues. We keep what we got and the
            # missing slots stay in English; user can read those few lines.
            print(f"[translator] chunk {start}-{start+expected}: gave up "
                  f"after 2 attempts, {len(missing)} cues will stay English",
                  flush=True)

        translations.update(valid)

    # Apply — any cue Haiku didn't translate (or that we rejected as
    # off-chunk) stays as the original English. Better partial Chinese
    # than corrupted alignment.
    for c in cues:
        new_lines = translations.get(c["idx"])
        if new_lines:
            c["lines"] = new_lines

    out_path.write_text(render_srt(cues), encoding="utf-8")
    stamp_source(out_path, "llm-translated")


def translate_video_zh(video: Path) -> None:
    """Top-level worker submitted by the bt translate-zh endpoint. Translate
    the sibling `<stem>.srt` (the English transcript bt left in place) to
    繁體中文 via Haiku.

    OpenSubtitles is NOT consulted — Chinese-sub uploads on OS are sparse
    and frequently mistagged for releases the user actually watches, so
    going straight to Haiku gives a more predictable result. Cost is
    ~$0.01 per movie.

    Output: `<stem>.zh-tw.srt` next to the video on success, or
    `<stem>.zh-tw.srt.error` with the failure reason if Haiku fails or
    the English SRT is missing.

    Idempotent: skips if `<stem>.zh-tw.srt` already exists. The endpoint
    pre-clears `.error` stamps before submitting, so calling the endpoint
    again counts as a retry.
    """
    zh_path = video.parent / f"{video.stem}{ZH_SUFFIX}"
    err_path = Path(str(zh_path) + ".error")
    eng_srt = video.with_suffix(".srt")

    if zh_path.exists():
        return  # already done

    if not eng_srt.exists():
        reason = (
            "no English SRT to translate from; the sibling <stem>.srt is "
            "missing. Did the bt pipeline produce one for this video?"
        )
        try:
            err_path.write_text(reason, encoding="utf-8")
        except OSError as e:
            print(f"[translate-zh] stamp .error failed for {video.name!r}: {e}", flush=True)
        return

    print(f"[translate-zh] translating {video.name!r} via Haiku", flush=True)
    try:
        translate_to_zh(eng_srt, zh_path)
        print(f"[translate-zh] wrote {zh_path.name!r}", flush=True)
    except Exception as exc:
        traceback.print_exc()
        try:
            err_path.write_text(f"Haiku translation failed: {exc}", encoding="utf-8")
        except OSError as e:
            print(f"[translate-zh] stamp .error failed for {video.name!r}: {e}", flush=True)
