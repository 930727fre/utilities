"""English-to-zh-TW SRT translator — Gemini Flash Lite fallback for the
bt "translate to zh" button when OpenSubtitles doesn't have a Chinese sub
for the release (most common cause: brand-new films, niche shows).

Reuses annotate.py's SRT parse → chunk → forced-JSON → render pattern.
Translation is a well-trodden, low-creativity task that the small
Gemini tier handles fine; quality on mainstream films is comparable to
the average OpenSubtitles user upload at a fraction of the wait.

Output SRT is structurally identical to the input — same cue indices,
same timestamps — just the dialogue lines replaced with 繁體中文 and the
existing sentinel cues (`※ source: …`, `※ annotated`, etc.) carried
through unchanged. A new `※ source: llm-translated` sentinel gets
appended; annotate.py's chronological sort on the next pass — wait,
annotate.py doesn't touch translate_zh outputs. The sentinel sits at
end of file with timestamp 00:00:00 so a player honors timestamp order;
casual `head` inspection sees `※ annotated` (from the carried-over
English SRT) but not our new tag. That's a known cosmetic; the SRT is
functionally correct.
"""
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from annotate import parse_srt, render_srt  # reuse the same parser/renderer
from gemini_client import generate_json
from srt_source import stamp_source

ZH_SUFFIX = ".zh-tw.srt"
ZH_LANGUAGES = "zh-tw,zh-cn"

# Single worker — translation is API-bound and we want predictable
# ordering across the file. Same shape as annotate_executor.
translator_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translator-worker")

# Cue count per call. Gemini Flash Lite handles 400 cues comfortably
# at the output token cap.
CHUNK_SIZE = 400

_MODEL = os.environ.get("GEMINI_TRANSLATE_MODEL", "gemini-2.5-flash-lite")

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

Rules:
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
- Lines that already contain `※` (our own sentinel markers like \
`※ source: …`, `※ annotated`): pass through UNCHANGED, do not translate, \
do not edit.
- Cue indices: use the integer index shown in the input.

OUTPUT JSON: array of {"cue": <int>, "lines": [<str>, ...]}. Include EVERY \
input cue — empty lines stay empty, sentinel cues pass through verbatim.

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
        prompt = _PROMPT_TEMPLATE % chunk_text
        result = generate_json(prompt, _SCHEMA, temperature=0.2, model=_MODEL)
        for entry in result:
            try:
                cue_idx = int(entry["cue"])
                lines = [str(ln) for ln in entry["lines"]]
            except (KeyError, ValueError, TypeError):
                continue
            if not lines:
                continue
            translations[cue_idx] = lines

    # Apply — keep any cue Gemini skipped as original English (defensive;
    # better partial Chinese than no SRT).
    for c in cues:
        new_lines = translations.get(c["idx"])
        if new_lines is not None:
            c["lines"] = new_lines

    out_path.write_text(render_srt(cues), encoding="utf-8")
    stamp_source(out_path, "llm-translated")


def translate_video_zh(video: Path) -> None:
    """Top-level worker submitted by the bt translate-zh endpoint. For one
    video: try OpenSubtitles for a human-translated zh-tw/zh-cn sub first;
    if OS misses for any reason, fall through to Gemini translating the
    sibling `<stem>.srt` (the English transcript bt left in place).

    Output: `<stem>.zh-tw.srt` next to the video on success, or
    `<stem>.zh-tw.srt.error` with the failure reason on either-step failure.

    Idempotent: skips if `<stem>.zh-tw.srt` already exists. The endpoint
    pre-clears `.error` stamps + the subs_finder cache before submitting,
    so calling the endpoint again counts as a retry.
    """
    from subs_finder import find_subs  # local: avoid import cycle on app boot

    zh_path = video.parent / f"{video.stem}{ZH_SUFFIX}"
    err_path = Path(str(zh_path) + ".error")
    eng_srt = video.with_suffix(".srt")

    if zh_path.exists():
        return  # already done

    # 1) OS — best quality when available (human upload).
    result = find_subs(video, languages=ZH_LANGUAGES, out_path=zh_path)
    if result is not None:
        print(f"[translate-zh] OS picked {zh_path.name!r}", flush=True)
        return

    # 2) Gemini fallback — needs the English transcript as source.
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

    print(f"[translate-zh] OS missed for {video.name!r}, using Gemini", flush=True)
    try:
        translate_to_zh(eng_srt, zh_path)
        print(f"[translate-zh] wrote {zh_path.name!r} via Gemini", flush=True)
    except Exception as exc:
        traceback.print_exc()
        try:
            err_path.write_text(f"Gemini translation failed: {exc}", encoding="utf-8")
        except OSError as e:
            print(f"[translate-zh] stamp .error failed for {video.name!r}: {e}", flush=True)
