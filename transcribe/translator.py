"""English-to-zh-TW SRT translator — small batches (10 cues) with
sliding context window + count/index validation. Drives the bt
"translate to 中" button via Gemini Flash Lite.

We've been through three architectures, this is the fourth:

  1. 400-200 cues per Flash Lite call — Flash Lite silently dropped
     short cues from JSON output and renumbered the rest. Subtitle
     content drifted N positions off the spoken dialogue.
  2. 200 cues per Haiku call — better at 'exactly N entries' but still
     hit content-shifts inside a chunk.
  3. 1 cue per call with 5+5 sliding context — structurally bulletproof
     alignment but 17:1 input/output token ratio (530 prompt tokens to
     translate one ~30-token cue). 818 API calls per episode.
  4. **10 cues per call with 3+3 sliding context (this)** — middle
     ground: ~10× fewer API calls than per-cue, ~3× cheaper token-wise,
     scope of any per-batch alignment failure capped at 10 cues, and a
     count+index validator + one retry catches the rare cases where
     Gemini drops a short cue.

Per call: ~700 input tokens (prompt + 6 context cues + 10 targets) +
~300 output tokens. Per episode (~818 cues → ~82 batches): ~57k input,
~25k output → ~$0.02. Per season (13 episodes): ~$0.25 (NT$8).

Output SRT is structurally identical to the input — same cue indices,
same timestamps — just the dialogue lines replaced with 繁體中文 and the
existing sentinel cues (`※ source: …`, `※ annotated`, etc.) carried
through unchanged. A new `※ source: llm-translated` sentinel gets
appended at the file end with timestamp 00:00:00.
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from annotate import parse_srt, render_srt  # reuse the same parser/renderer
from gemini_client import generate_json

ZH_SUFFIX = ".zh-tw.srt"

# How many cues per Gemini call. Larger batches cut API calls + cost but
# raise alignment risk (Gemini occasionally drops short cues from
# array-output schemas, shifting the rest). 10 is the empirical sweet
# spot: blast radius of any per-batch failure is bounded to 10 cues, and
# the count+index validator catches misalignment in time to retry.
BATCH_SIZE = 10

# Batch-level parallelism inside translate_to_zh. A ~1-hour episode has
# ~80 batches at ~2-3 s each; 10 concurrent brings wall-clock from
# 3-4 min to 20-30 s per episode. Not an "annotate-style single call"
# situation — annotate is 1 call/episode, translate is 80 calls/episode
# and must fan-out to be usable at wrapper scale.
_BATCH_CONCURRENCY = 10

# Sliding context: N cues before the batch and N after, as REFERENCE
# only (not translated). The batch itself provides 10 cues of internal
# context, so we keep the additional window small to control prompt
# size while still resolving pronouns / speaker / emotional beat across
# the boundary.
_CONTEXT_BEFORE = 3
_CONTEXT_AFTER = 3

_MODEL = os.environ.get("TRANSLATE_MODEL", "gemini-3.1-flash-lite")

# Module-level session for connection reuse across the concurrent
# batches within a translate_to_zh call. Default `requests.Session`
# caps the per-host pool at 10 — matches _BATCH_CONCURRENCY so no
# in-flight batch has to wait for a socket, but explicit sizing keeps
# the contract obvious if _BATCH_CONCURRENCY ever changes.
_http_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=_BATCH_CONCURRENCY,
                       pool_maxsize=_BATCH_CONCURRENCY)
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)


def _get_session() -> requests.Session:
    return _http_session


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
You translate English SRT cues into 繁體中文 (Taiwan).

The "context" cues before and after are REFERENCE ONLY — they show the \
surrounding dialogue so you can resolve pronouns, speaker, and emotional \
beat. Do not translate them, do not include them in your output.

The "TARGETS" are the %d cues to translate.

ALIGNMENT INTEGRITY (critical):
- Output EXACTLY %d entries in the JSON array, one per input target cue, \
in input order.
- Use the EXACT integer `cue` index shown in each TARGET line. Do not \
renumber, increment, or invent new indices.
- DO NOT drop, merge, skip, or combine cues — not even very short ones \
("Yeah.", "Asshole!", "Mm-hmm.", interjections). Each target must \
appear as its own entry, even if the dialogue is one word.

Translation rules:
- Translate the dialogue faithfully and naturally in Taiwanese idiom; do \
not add explanations or expand.
- Preserve the line break structure within each cue: a 2-line target \
returns 2 strings in `lines`; 1-line returns 1.
- SDH-style audio annotations like `[applause]`, `[music]`, `[door slams]` \
→ translate concisely as 「[掌聲]」「[音樂]」「[關門聲]」.
- Song lyrics (lines marked with `♪` or all-caps singing): translate the \
meaning naturally; keep `♪` markers if present.
- Speaker labels (e.g. `MICHAEL:`, `-CARMELA:`): keep the label format but \
translate the dialogue part. Names stay in Roman script.
- Personal names inside dialogue (Tony, Carmela, Christopher, Junior, \
Dr. Cusamano, Pussy, etc.): keep in the original Roman script verbatim — \
DO NOT transliterate into Chinese characters (write "Tony" not "東尼", \
"Carmela" not "卡梅拉"). Place names follow the same rule unless they \
are canonical 繁體中文 terms (e.g. "New York" stays "紐約" because that \
is the standard rendering, but "Newark" stays "Newark").
- Lines that already contain `※` (our own sentinel markers like \
`※ source: …`, `※ annotated`, or cultural-context notes prepended by \
the annotator): output that specific line UNCHANGED. This applies \
per-line, not per-cue: if a cue has 2 lines where one is English \
dialogue and the other starts with `※`, you MUST translate the English \
dialogue line into 繁體中文 AND output the `※` line verbatim. NEVER \
skip translating an English dialogue line just because a sibling line \
in the same cue contains `※`.
- NEVER include the original English in your output unless the line \
itself starts with `※`. The output is a Chinese-only subtitle track — \
returning English dialogue verbatim is a translation failure. If a cue \
is short ("Hey", "Yeah", single-word interjections), translate it; do \
not pass through.

Output JSON: array of {"cue": <int>, "lines": [<str>, ...]} — EXACTLY \
%d entries, one per input target cue, in input order.

%s

>>> TARGETS (translate these %d cues):
%s
<<< END TARGETS

%s
"""


def _render_cues_block(label: str, cues: list[dict]) -> str:
    if not cues:
        return f"{label}: (none)"
    body = "\n\n".join(f"[cue {c['idx']}]\n" + "\n".join(c["lines"]) for c in cues)
    return f"{label}:\n{body}"


def _translate_batch(cues: list[dict], batch_positions: list[int]) -> dict[int, list[str]]:
    """Translate the cues at `batch_positions` within `cues`. Returns a
    map from cue index (the SRT cue number, not the list position) to
    the translated lines. Missing entries (Gemini dropped them across
    both attempts) are simply absent from the map — caller leaves those
    cues in English.

    Robustness:
      - On the first attempt, accept any entry whose `cue` index is one
        of the targets'.
      - If Gemini's response is missing any target indices, retry once.
      - After 2 attempts, keep what we got. Partial Chinese beats
        corrupted alignment.
    """
    first = batch_positions[0]
    last = batch_positions[-1]
    targets = [cues[i] for i in batch_positions]
    before = cues[max(0, first - _CONTEXT_BEFORE):first]
    after = cues[last + 1:last + 1 + _CONTEXT_AFTER]

    target_indices = {c["idx"] for c in targets}
    n = len(targets)

    before_block = _render_cues_block("Context BEFORE", before)
    after_block = _render_cues_block("Context AFTER", after)
    targets_block = "\n\n".join(f"[cue {c['idx']}]\n" + "\n".join(c["lines"]) for c in targets)

    prompt = _PROMPT_TEMPLATE % (n, n, n, before_block, n, targets_block, after_block)

    valid: dict[int, list[str]] = {}
    for attempt in range(2):
        try:
            result = generate_json(prompt, _SCHEMA, temperature=0.0, model=_MODEL,
                                   session=_get_session())
        except Exception as e:
            print(f"[translator] batch cues {first}-{last} attempt {attempt+1}/2 "
                  f"raised: {e}", flush=True)
            continue

        valid = {}
        for entry in result:
            try:
                cue_idx = int(entry["cue"])
                lines = [str(ln) for ln in entry["lines"]]
            except (KeyError, ValueError, TypeError):
                continue
            if cue_idx not in target_indices:
                continue
            valid[cue_idx] = lines
        missing = target_indices - valid.keys()
        if not missing:
            return valid
        print(f"[translator] batch cues {first}-{last}: Gemini returned "
              f"{len(valid)}/{n} (missing {len(missing)}); attempt {attempt+1}/2",
              flush=True)

    return valid  # partial after 2 attempts; caller keeps missing cues in English


def translate_to_zh(src_srt: Path, out_path: Path) -> None:
    """Read `src_srt`, translate each cue's dialogue to 繁體中文 in
    batches of `BATCH_SIZE`, write the result to `out_path`. Raises on
    parser / write failure; per-batch API failures silently fall back
    to the original English for the missing cues."""
    try:
        raw = src_srt.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = src_srt.read_text(encoding="latin-1")

    cues = parse_srt(raw)
    if not cues:
        raise RuntimeError(f"source SRT had no parseable cues: {src_srt}")

    # Pass `※`-prefixed sentinel cues through verbatim. They aren't real
    # dialogue, and sending them to the model wastes tokens (and risks
    # the model interpreting them as something to translate).
    def is_sentinel(cue: dict) -> bool:
        visible = [ln.strip() for ln in cue.get("lines", []) if ln.strip()]
        return bool(visible) and all(ln.startswith("※") for ln in visible)

    translate_positions = [i for i, c in enumerate(cues) if not is_sentinel(c)]
    # Split the work into contiguous batches of BATCH_SIZE. We batch on
    # POSITIONS (indices in `cues`) not SRT cue indices, because cue
    # numbering in the input SRT isn't guaranteed contiguous (sentinels
    # use 99999 etc.) — position keeps batches dense without gaps.
    batches = [translate_positions[i:i + BATCH_SIZE]
               for i in range(0, len(translate_positions), BATCH_SIZE)]

    # Fan out batches — they're independent (each carries its own
    # sliding-window context), so pool concurrency gives roughly linear
    # speedup up to the Gemini quota / per-host connection pool.
    # Per-batch exceptions are swallowed as empty dict: missing cues
    # keep their English line, matching partial-coverage semantics
    # elsewhere.
    translations: dict[int, list[str]] = {}
    with ThreadPoolExecutor(max_workers=_BATCH_CONCURRENCY,
                             thread_name_prefix="translator-batch") as pool:
        futures = [pool.submit(_translate_batch, cues, batch) for batch in batches]
        for fut in as_completed(futures):
            try:
                batch_result = fut.result()
            except Exception:
                batch_result = {}
            translations.update(batch_result)

    # Apply translations back. Cues we didn't translate (sentinels) or
    # cues Gemini dropped keep their original English lines — partial
    # Chinese still beats no SRT.
    for c in cues:
        new_lines = translations.get(c["idx"])
        if new_lines:
            c["lines"] = new_lines

    out_path.write_text(render_srt(cues), encoding="utf-8")


def translate_video_zh(video: Path) -> None:
    """Translate the sibling `<stem>.srt` to 繁體中文 via 10-cue Gemini
    Flash Lite batches with sliding-window context, writing
    `<stem>.zh-tw.srt` next to the video.

    Called inline as the final stage of process_bt_file — English SRT
    alone isn't consumable for the user, so the pipeline isn't "done"
    until this succeeds. Failure raises; the caller stamps the standard
    `.pipeline-failed` sidecar (same shape as any other pipeline-stage
    failure).

    OpenSubtitles is NOT consulted — Chinese-sub uploads on OS are sparse
    and frequently mistagged for releases the user actually watches.

    Idempotent: skips silently if `<stem>.zh-tw.srt` already exists, so
    a crash-recovered pipeline that already produced the zh SRT won't
    burn API cost re-translating.
    """
    zh_path = video.parent / f"{video.stem}{ZH_SUFFIX}"
    if zh_path.exists():
        return  # already done — idempotent skip for crash recovery

    eng_srt = video.with_suffix(".srt")
    if not eng_srt.exists():
        # Caller (process_bt_file) writes canonical before calling us,
        # so this shouldn't happen except in bizarre races. Raise so the
        # standard pipeline-failure path handles it.
        raise RuntimeError(
            f"no English SRT at {eng_srt} to translate from — "
            "canonical write must have failed silently"
        )

    print(f"[translate-zh] translating {video.name!r} via Gemini (10-cue batches)", flush=True)
    translate_to_zh(eng_srt, zh_path)
    print(f"[translate-zh] wrote {zh_path.name!r}", flush=True)
    # Mirror to data/archive/ so a future delete_torrent + re-download
    # can reuse the Chinese SRT alongside the English one (see archive.py).
    from archive import mirror_to_archive
    mirror_to_archive(zh_path)
