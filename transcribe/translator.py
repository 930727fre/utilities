"""English-to-zh-TW SRT translator — per-cue + sliding context window via
Gemini Flash Lite. Drives the bt "translate to 中" button.

We had two earlier architectures, both with alignment failure modes:

  - **Batch chunks of 200 cues, one Flash Lite call per chunk**: Flash Lite
    silently dropped short cues ("Yeah.", interjections, profanity) from
    its JSON output. Every cue after the dropped one inherited the wrong
    translation, since our cue indexing read from Flash Lite's response.
    Visible subtitle text drifted off the spoken dialogue by N positions
    until the next chunk boundary reset alignment.
  - **Batch chunks via Haiku**: better at the "exactly N entries" rule
    but still hit content-shifts inside a chunk — the entry count would
    be correct and indices would span the chunk, but specific entries'
    content would silently be one cue ahead/behind.

Both shared the same root cause: asking an LLM to output a structured
array where each entry must independently correspond to a specific input
by index. The model maintains array-level invariants well enough for
short batches, but not perfectly across hundreds of items.

This module takes the industry-standard route: **one cue per LLM call**.
There's no array, so there's no array-internal misalignment possible —
the response is unambiguously the translation of the one cue we sent.
For context-aware quality, each call includes a sliding window of 5
preceding + 5 following cues as REFERENCE (not to translate, only to
inform the target). The model sees pronouns, who's speaking, the
emotional beat — without the structural risk of batched output.

  Per call: ~600 input tokens (prompt + 11 cues × ~30 each) + ~30 output
  Per episode (818 cues): ~6 input M-tokens, ~30k output tokens → ~$0.06
  Per season (13 episodes): ~$0.80 (NT$24)

Output SRT is structurally identical to the input — same cue indices,
same timestamps — just the dialogue lines replaced with 繁體中文 and the
existing sentinel cues (`※ source: …`, `※ annotated`, etc.) carried
through unchanged. A new `※ source: llm-translated` sentinel gets
appended at the file end with timestamp 00:00:00.
"""
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from annotate import parse_srt, render_srt  # reuse the same parser/renderer
from gemini_client import generate_json
from srt_source import stamp_source

ZH_SUFFIX = ".zh-tw.srt"

# Two workers — Gemini Flash Lite is fast and Tier 2 limits are generous,
# but two parallel episodes is plenty given the cue-level concurrency
# inside each one. (See _CUE_CONCURRENCY below.)
translator_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="translator-worker")

# Cue-level parallelism INSIDE translate_to_zh. Host-side benchmarking
# with the real prompt size showed RPS peaks at ~20 parallel (15.4 RPS)
# and DROPS at 30 (13.4 RPS) — Google starts throttling burstier traffic
# even within the per-minute quota. 20 sits in the sweet spot.
_CUE_CONCURRENCY = 20

# Number of context cues on each side of the target. 5+5 gives enough
# surrounding dialogue for the model to resolve pronouns / speaker
# changes / emotional tone without bloating each prompt.
_CONTEXT_BEFORE = 5
_CONTEXT_AFTER = 5

_MODEL = os.environ.get("TRANSLATE_MODEL", "gemini-2.5-flash-lite")

# Shared HTTP session with an enlarged connection pool. The default
# `requests` module-level session caps at 10 connections per host, so 30
# concurrent translation threads would actually only get 10 sockets and
# queue for the rest — measured 6x slowdown vs theoretical (per-call
# latency ~1 s, 818 cues / 30 should finish in ~30 s, observed ~3 min).
# Bumping pool_maxsize to 2× _CUE_CONCURRENCY × max_workers leaves
# comfortable headroom.
_http_session: requests.Session | None = None
_http_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _http_session
    if _http_session is not None:
        return _http_session
    with _http_session_lock:
        if _http_session is None:
            s = requests.Session()
            pool_size = _CUE_CONCURRENCY * 2 * 2  # cue concurrency × episode workers × 2 headroom
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            _http_session = s
    return _http_session

# Schema for one cue's translation: a list of dialogue lines mirroring
# the input cue's line count.
_SCHEMA = {
    "type": "object",
    "properties": {
        "translation": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["translation"],
}

_PROMPT_TEMPLATE = """\
You translate one English subtitle cue into 繁體中文 (Taiwan).

The "target" is the only cue you translate. The "context" cues before \
and after it are REFERENCE ONLY — they show the surrounding dialogue so \
you can resolve pronouns, speaker, and emotional beat. Do not translate \
them, do not include them in your output.

Translation rules:
- Translate the target faithfully and naturally in Taiwanese idiom; do \
not add explanations or expand.
- Preserve the line break structure: if target has 2 lines, your \
"translation" array has 2 strings; if 1 line, 1 string.
- SDH-style audio annotations like `[applause]`, `[music]`, \
`[door slams]` → translate concisely as 「[掌聲]」「[音樂]」「[關門聲]」.
- Song lyrics (lines marked with `♪` or all-caps singing): translate the \
meaning naturally; keep `♪` markers if present in the source.
- Speaker labels (e.g. `MICHAEL:`, `-CARMELA:`): keep the label format \
but translate the dialogue part. Names stay in Roman script.
- Personal names inside dialogue (Tony, Carmela, Christopher, Junior, \
Dr. Cusamano, Pussy, etc.): keep in the original Roman script verbatim — \
DO NOT transliterate into Chinese characters (write "Tony" not "東尼", \
"Carmela" not "卡梅拉"). Place names follow the same rule unless they \
are canonical 繁體中文 terms (e.g. "New York" stays "紐約" because that \
is the standard rendering, but "Newark" stays "Newark").
- Lines that already contain `※` (our own sentinel markers like \
`※ source: …`, `※ annotated`): output the line UNCHANGED, do not \
translate, do not edit.

Output JSON: {"translation": ["<line1>", "<line2>", ...]} — array \
length equals target line count.

%s

>>> TARGET (translate this one):
%s
<<< END TARGET

%s
"""


def _render_context_block(before: list[dict], after: list[dict]) -> tuple[str, str]:
    """Render context-before and context-after blocks for the prompt."""
    def fmt(label: str, ctx: list[dict]) -> str:
        if not ctx:
            return f"{label}: (none)"
        body = "\n".join("\n".join(c["lines"]) for c in ctx)
        return f"{label}:\n{body}"
    return fmt("Context BEFORE", before), fmt("Context AFTER", after)


def _translate_one_cue(cues: list[dict], idx: int) -> list[str] | None:
    """Translate cue at position `idx` in `cues`. Returns the translated
    lines, or None if the API failed (caller keeps the original)."""
    target = cues[idx]
    target_text = "\n".join(target["lines"])
    before = cues[max(0, idx - _CONTEXT_BEFORE):idx]
    after = cues[idx + 1:idx + 1 + _CONTEXT_AFTER]

    before_block, after_block = _render_context_block(before, after)
    prompt = _PROMPT_TEMPLATE % (before_block, target_text, after_block)

    try:
        result = generate_json(prompt, _SCHEMA, temperature=0.0, model=_MODEL,
                               session=_get_session())
    except Exception as e:
        print(f"[translator] cue {target['idx']} failed: {e}", flush=True)
        return None

    lines = result.get("translation")
    if not isinstance(lines, list) or not lines:
        return None
    return [str(ln) for ln in lines]


def translate_to_zh(src_srt: Path, out_path: Path) -> None:
    """Read `src_srt`, translate each cue's dialogue to 繁體中文 via a
    per-cue Gemini call with sliding-window context, write the result to
    `out_path`. Raises on parser / write failure; per-cue API failures
    silently fall back to the original English for that cue."""
    try:
        raw = src_srt.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = src_srt.read_text(encoding="latin-1")

    cues = parse_srt(raw)
    if not cues:
        raise RuntimeError(f"source SRT had no parseable cues: {src_srt}")

    # Pass `※`-prefixed sentinel cues through verbatim. They aren't real
    # dialogue, and sending them to the model wastes a call (and risks
    # the model interpreting them as something to translate). Identify by
    # the same rule annotate.py uses.
    def is_sentinel(cue: dict) -> bool:
        visible = [ln.strip() for ln in cue.get("lines", []) if ln.strip()]
        return bool(visible) and all(ln.startswith("※") for ln in visible)

    indices_to_translate = [i for i, c in enumerate(cues) if not is_sentinel(c)]

    # Per-cue Gemini calls run in parallel inside this episode worker.
    # Each call independently translates one cue with its sliding context;
    # the result writes to translations[idx] in any order, then we apply
    # back to cues in original order at the end.
    translations: dict[int, list[str]] = {}
    with ThreadPoolExecutor(max_workers=_CUE_CONCURRENCY,
                             thread_name_prefix="translator-cue") as pool:
        futures = {pool.submit(_translate_one_cue, cues, i): i
                   for i in indices_to_translate}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                lines = fut.result()
            except Exception:
                lines = None
            if lines:
                translations[i] = lines

    # Apply translations back. Cues we didn't translate (sentinels) or
    # whose API call failed keep their original English lines — partial
    # Chinese still beats no SRT.
    for i, c in enumerate(cues):
        new_lines = translations.get(i)
        if new_lines:
            c["lines"] = new_lines

    out_path.write_text(render_srt(cues), encoding="utf-8")
    stamp_source(out_path, "llm-translated")


def translate_video_zh(video: Path) -> None:
    """Top-level worker submitted by the bt translate-zh endpoint. Translate
    the sibling `<stem>.srt` (the English transcript bt left in place) to
    繁體中文 via per-cue Gemini Flash Lite with sliding-window context.

    OpenSubtitles is NOT consulted — Chinese-sub uploads on OS are sparse
    and frequently mistagged for releases the user actually watches.

    Output: `<stem>.zh-tw.srt` next to the video on success, or
    `<stem>.zh-tw.srt.error` with the failure reason if translation fails
    or the English SRT is missing.

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

    print(f"[translate-zh] translating {video.name!r} via per-cue Gemini", flush=True)
    try:
        translate_to_zh(eng_srt, zh_path)
        print(f"[translate-zh] wrote {zh_path.name!r}", flush=True)
    except Exception as exc:
        traceback.print_exc()
        try:
            err_path.write_text(f"translation failed: {exc}", encoding="utf-8")
        except OSError as e:
            print(f"[translate-zh] stamp .error failed for {video.name!r}: {e}", flush=True)
