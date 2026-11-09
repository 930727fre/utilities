# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "elevenlabs>=2.0.0",
#     "opencc-python-reimplemented>=0.1.7",
# ]
# ///
"""ElevenLabs Scribe v2 講者分離 + 轉錄 + LLM 錯字修正 prompt 產生器。

雙用途：
1. CLI（uv 直接跑）：
       uv run transcribe.py [audio_file.m4a]
   會把 prompt 寫到 <basename>_correction_prompt.txt。

2. Module（server.py 用）：
       from transcribe import get_diarized_transcript, build_correction_prompt
       audio_bytes = open("foo.m4a", "rb").read()
       transcript, resolved_lang = get_diarized_transcript(audio_bytes)
       prompt = build_correction_prompt(transcript, language=resolved_lang)

get_diarized_transcript 收 bytes 不是 path — 這樣 CLI 跟 web upload
兩條路都能餵，不用先把 upload 落地成暫存檔。

Language handling: three selectable values — "zh-TW", "en", or the
AUTO_LANGUAGE sentinel "multi" (now the default). An explicit zh-TW/en
pick is passed straight through as ElevenLabs' `language_code` hint
(their own docs: an explicit code "can improve transcription
performance if known beforehand"). "multi" omits `language_code`
entirely — Scribe auto-detects — and after the call we read back
Scribe's own `response.language_code` to resolve which of our two
presentation buckets (zh-TW / en) to use for the speaker-label wording
and the correction-prompt template. Both get_diarized_transcript and
get_plain_transcript return `(text, resolved_language)` so callers
always know which bucket was actually used, even when the caller
asked for "multi".

Chinese output normalization: ElevenLabs' `language_code` only accepts
generic "zh" (no zh-TW/zh-CN variant — that's an OpenCC-input, not an
ASR-controllable dimension), and Scribe's actual output script isn't
documented/guaranteed either way. Rather than gate traditionalization
on the resolved language bucket, every transcript (regardless of
language/resolved bucket) is run through OpenCC (s2t) unconditionally
— its conversion table only touches Han characters, so Latin text,
digits, and punctuation pass through untouched. This also correctly
normalizes Chinese *segments* embedded in an otherwise-English or
mixed transcript, which a language-gated approach would miss.
"""

import io
import os
import re
import sys

from elevenlabs import ElevenLabs
from opencc import OpenCC

STT_MODEL = "scribe_v2"

# Sentinel meaning "let Scribe auto-detect the language" — omit
# `language_code` from the API call, resolve afterwards from
# `response.language_code`. Selectable, but NOT the default: tested
# against real bilingual zh/en recordings, an explicit "zh" hint
# transcribes code-switched English segments correctly too (it's a
# soft bias toward the expected language, not an exclusive filter) —
# so for content that's primarily Chinese with English mixed in
# (this user's actual typical case), the explicit pick already works
# well and doesn't need auto-detect. "multi" stays available for
# content that genuinely isn't Chinese-primary or is unknown.
AUTO_LANGUAGE = "multi"
DEFAULT_LANGUAGE = "zh-TW"

# Our own language codes → (ElevenLabs language_code, speaker label).
# ElevenLabs uses plain ISO-639-1 "zh" for Chinese (no region variant).
# Public (no leading underscore) — server.py imports this for its
# language allowlist check, the same role SPEAKER_LABELS played in
# the Deepgram-era code. AUTO_LANGUAGE is deliberately NOT a key here
# — it isn't a resolvable (api_code, speaker_label) pair up front, it
# only resolves to one of these two *after* the API call returns.
LANGUAGES = {
    "zh-TW": {"api_code": "zh", "speaker_label": "說話者"},
    "en": {"api_code": "en", "speaker_label": "Speaker"},
}

_S2T = OpenCC("s2t")


def _traditionalize(text: str) -> str:
    """Force any Han characters in `text` to Traditional via OpenCC
    (s2t). Safe to call unconditionally on any text, any language,
    any mix — the conversion table only touches Han characters; Latin
    letters, digits, and punctuation pass through untouched. A no-op
    if the text has no Simplified characters to begin with."""
    return _S2T.convert(text)


def _resolve_effective_language(requested: str, detected_code: str | None) -> str:
    """An explicit zh-TW/en pick always wins outright — that's a
    deliberate choice, not a guess, and we honor it even if Scribe's
    own detection would've said otherwise. Only when `requested` is
    AUTO_LANGUAGE do we fall back to whatever Scribe detected,
    bucketed into our two known presentation languages. Everything
    that isn't Chinese defaults to the English bucket — we only
    maintain two correction-prompt templates / speaker-label
    wordings, not one per possible detected language."""
    if requested != AUTO_LANGUAGE:
        return requested
    code = (detected_code or "").lower()
    if code.startswith("zh") or code == "cmn":
        return "zh-TW"
    return "en"


_SPEAKER_ID_RE = re.compile(r"^speaker_?(\d+)$", re.IGNORECASE)


def _speaker_display(speaker_id: str | None, fallback_index: int) -> str:
    """ElevenLabs speaker_id looks like "speaker_0" / "speaker_1". Strip
    to the bare number so labels read "[Speaker 0]" like the old
    Deepgram-backed output did. Falls back to a running index if
    speaker_id is missing (shouldn't happen with diarize=True, but
    don't crash the whole transcript over a formatting detail)."""
    if speaker_id:
        m = _SPEAKER_ID_RE.match(speaker_id)
        if m:
            return m.group(1)
        return speaker_id
    return str(fallback_index)


def get_diarized_transcript(
    audio_bytes: bytes,
    language: str = DEFAULT_LANGUAGE,
    num_speakers: int | None = None,
    filename: str = "audio.m4a",
) -> tuple[str, str]:
    """Transcribe `audio_bytes` with speaker diarization, return
    `(transcript, resolved_language)`. `transcript` is a
    "[Speaker N] text" transcript, one line per speaker turn.
    `resolved_language` is `language` unchanged for an explicit
    zh-TW/en pick, or Scribe's own detected language bucketed into
    zh-TW/en when `language` was AUTO_LANGUAGE — callers that need to
    pick a correction-prompt template use this, not the original
    `language` argument.

    `num_speakers`: exact speaker count hint (1-32) if known, None to
    let Scribe auto-detect. Mirrors the API's own `num_speakers`
    semantics — see ElevenLabs STT docs.

    `filename`: used only to give the uploaded bytes a real extension
    in the multipart part (a bare BytesIO has none). Scribe's own
    format-detection likely sniffs content either way, but a correct
    extension costs nothing and removes one variable if something
    ever goes wrong container-wise. Default is a harmless placeholder
    for callers (e.g. tests) that don't care.
    """
    if not os.getenv("ELEVENLABS_API_KEY"):
        raise RuntimeError("找不到 ELEVENLABS_API_KEY，請先設定環境變數。")
    if language != AUTO_LANGUAGE and language not in LANGUAGES:
        raise ValueError(
            f"Unsupported language: {language!r} "
            f"(expected {AUTO_LANGUAGE!r} or one of {list(LANGUAGES)})"
        )
    if num_speakers is not None and not (1 <= num_speakers <= 32):
        raise ValueError(f"num_speakers must be 1-32, got {num_speakers}")

    buf = io.BytesIO(audio_bytes)
    buf.name = filename

    client = ElevenLabs()
    kwargs = dict(
        file=buf,
        model_id=STT_MODEL,
        diarize=True,
        tag_audio_events=False,
        timestamps_granularity="word",
    )
    if language != AUTO_LANGUAGE:
        kwargs["language_code"] = LANGUAGES[language]["api_code"]
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers
    response = client.speech_to_text.convert(**kwargs)

    resolved = _resolve_effective_language(language, getattr(response, "language_code", None))
    speaker_label = LANGUAGES[resolved]["speaker_label"]

    lines = []
    current_speaker_id: str | None = None
    current_speaker_display: str | None = None
    current_text: list[str] = []
    speaker_seen = 0

    for item in response.words:
        # "audio_event" items (laughter, background noise, ...) only
        # show up when tag_audio_events=True; skip defensively anyway
        # — this transcript format is speech-only, matching the old
        # Deepgram-backed output.
        if item.type == "audio_event":
            continue
        # "spacing" items carry inter-word whitespace/punctuation and
        # often have no speaker_id of their own — inherit whichever
        # speaker is currently open rather than starting a spurious
        # new segment.
        speaker_id = item.speaker_id or current_speaker_id
        if speaker_id != current_speaker_id:
            if current_text:
                lines.append(f"[{speaker_label} {current_speaker_display}] {''.join(current_text)}")
            current_speaker_id = speaker_id
            speaker_seen += 1
            current_speaker_display = _speaker_display(speaker_id, speaker_seen - 1)
            current_text = [item.text]
        else:
            current_text.append(item.text)

    if current_text:
        lines.append(f"[{speaker_label} {current_speaker_display}] {''.join(current_text)}")

    transcript = "\n".join(lines)
    return _traditionalize(transcript), resolved


def get_plain_transcript(
    audio_bytes: bytes,
    language: str = DEFAULT_LANGUAGE,
    filename: str = "audio.webm",
) -> tuple[str, str]:
    """Transcribe `audio_bytes` with NO diarization — just the text.
    Returns `(transcript, resolved_language)` — see
    `get_diarized_transcript`'s docstring for what `resolved_language`
    means.

    For the Dictate tab: one person talking to their own mic (the
    ChatGPT/Claude/Gemini-app voice-input pattern), where per-speaker
    segmentation is pure noise rather than useful structure. Unlike
    `get_diarized_transcript`, we don't walk `response.words` to
    reconstruct speaker turns — ElevenLabs already hands back the full
    transcript as a single string in `response.text`, diarized or not.
    """
    if not os.getenv("ELEVENLABS_API_KEY"):
        raise RuntimeError("找不到 ELEVENLABS_API_KEY，請先設定環境變數。")
    if language != AUTO_LANGUAGE and language not in LANGUAGES:
        raise ValueError(
            f"Unsupported language: {language!r} "
            f"(expected {AUTO_LANGUAGE!r} or one of {list(LANGUAGES)})"
        )

    buf = io.BytesIO(audio_bytes)
    buf.name = filename

    client = ElevenLabs()
    kwargs = dict(file=buf, model_id=STT_MODEL, diarize=False, tag_audio_events=False)
    if language != AUTO_LANGUAGE:
        kwargs["language_code"] = LANGUAGES[language]["api_code"]
    response = client.speech_to_text.convert(**kwargs)

    resolved = _resolve_effective_language(language, getattr(response, "language_code", None))
    return _traditionalize(response.text), resolved


def build_correction_prompt(transcript: str, language: str = "zh-TW") -> str:
    """`language` must be a resolved value ("zh-TW" or "en") — never
    pass AUTO_LANGUAGE here. Callers get the resolved bucket back from
    get_diarized_transcript/get_plain_transcript precisely so this
    never has to guess."""
    if language == "en":
        return f"""Below is a diarized transcript produced by speech recognition.
It may contain misheard words, misrecognized proper nouns, or misplaced punctuation.

Please identify and correct likely recognition errors based on context, with these rules:

1. Keep the original speaker segmentation and order — do not change them
2. Only correct clear recognition errors (misheard words, proper nouns, misplaced punctuation), do not rewrite tone or meaning
3. If unsure whether something is an error, leave it as-is — do not guess
4. Output the full corrected transcript in the same format: "[Speaker X] segment text", one line per segment
5. Optionally list a "correction diff" (original -> corrected) at the end so I can verify

=== Transcript ===
{transcript}

Please output the corrected transcript."""

    # zh-TW default
    return f"""以下是一段語音辨識產生的逐字稿，包含講者分段。
語音辨識可能會有同音字、中英混合詞辨識錯誤、專有名詞誤植等問題。

請你根據上下文語意，抓出可能辨識錯誤的字詞並修正，規則如下：

1. 保留原本的講者分段與講者順序，不要更動
2. 只修正明顯的辨識錯誤（同音錯字、詞語誤植、標點錯置等），不要改寫語氣或句意
3. 如果不確定某處是否為錯字，保留原文，不要亂猜
4. 修正後請直接輸出完整逐字稿，格式維持「[說話者 X] 該段文字」，一行一段
5. 若方便的話，可以在逐字稿後面另外列出「修改對照表」（原文 -> 修正後），方便我確認

=== 逐字稿 ===
{transcript}

請輸出修正後的逐字稿。"""


if __name__ == "__main__":
    audio_file = sys.argv[1] if len(sys.argv) > 1 else "test.m4a"
    # Optional 2nd arg selects language (en / zh-TW / multi). Defaults
    # to multi (auto-detect).
    language = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_LANGUAGE
    # Optional 3rd arg: exact speaker count. Omit for auto-detect.
    num_speakers = int(sys.argv[3]) if len(sys.argv) > 3 else None

    print(f"呼叫 ElevenLabs Scribe 轉錄中：{audio_file} (language={language}, num_speakers={num_speakers}) ...")
    with open(audio_file, "rb") as f:
        transcript, resolved_lang = get_diarized_transcript(
            f.read(), language=language, num_speakers=num_speakers,
            filename=os.path.basename(audio_file),
        )
    print(f"(resolved language: {resolved_lang})")

    print("\n" + "=" * 50)
    print("轉錄結果")
    print("=" * 50)
    print(transcript)

    prompt = build_correction_prompt(transcript, language=resolved_lang)

    print("\n" + "=" * 50)
    print("可直接複製貼給 LLM 的錯字修正 Prompt")
    print("=" * 50)
    print(prompt)

    out_path = os.path.splitext(audio_file)[0] + "_correction_prompt.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    print(f"\n已將完整 Prompt 儲存至 {out_path}，可直接開啟複製")
