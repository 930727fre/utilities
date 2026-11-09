"""FastAPI wrapper around transcribe.py.

Serves the upload UI at /, handles:
  POST /transcribe          — single-file diarized transcript + prompt
                               (speaker count always auto-detected)
  POST /transcribe-meeting  — same pipeline, but num_speakers is
                               caller-specified (optional)
  POST /transcribe-voice    — no diarization, no correction prompt.
                               For the Dictate tab: browser records
                               the mic directly and uploads the blob
                               right after stopping (ChatGPT/Claude/
                               Gemini-app voice-input pattern) — one
                               speaker, just the text, nothing to pair
                               an LLM-correction prompt against.

All three accept `language` as "zh-TW", "en", or "multi" (default —
see transcribe.AUTO_LANGUAGE) to let Scribe auto-detect. Responses
always include `detected_language` (the resolved zh-TW/en bucket —
unchanged from the request when a specific language was picked,
Scribe's own detection when "multi" was requested) so the frontend can
show what it actually used.

/transcribe and /transcribe-meeting return { transcript, prompt,
detected_language }; /transcribe-voice returns { transcript,
detected_language }. ELEVENLABS_API_KEY stays server-side — never
returned to the browser.
"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from transcribe import (
    AUTO_LANGUAGE,
    DEFAULT_LANGUAGE,
    LANGUAGES,
    build_correction_prompt,
    get_diarized_transcript,
    get_plain_transcript,
)

app = FastAPI()

INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

# Matches the cloudflared free-tier tunnel body cap (100 MB). Fine
# for manual review-workflow recordings; anything longer belongs in
# a batch pipeline, not a browser upload.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

MAX_SPEAKERS = 32


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


async def _read_and_validate_upload(file: UploadFile) -> bytes:
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(400, "空檔案")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"檔案 {len(audio_bytes) // (1024 * 1024)} MB 超過 "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限",
        )
    return audio_bytes


def _validate_language(language: str) -> None:
    # Explicit allowlist — never pass arbitrary user input straight to
    # the ElevenLabs language_code param. AUTO_LANGUAGE ("multi") is
    # a meta-value (resolved after the API call, not a real
    # language_code), so it's checked separately from LANGUAGES.
    if language != AUTO_LANGUAGE and language not in LANGUAGES:
        raise HTTPException(400, f"不支援的語言：{language}")


def _parse_num_speakers(raw: Optional[str]) -> Optional[int]:
    """Empty / missing → None (auto-detect). Otherwise must be a valid
    int in [1, MAX_SPEAKERS] — Scribe's own documented ceiling."""
    if raw is None or raw.strip() == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        raise HTTPException(400, f"人數必須是數字，收到：{raw!r}")
    if not (1 <= n <= MAX_SPEAKERS):
        raise HTTPException(400, f"人數必須介於 1-{MAX_SPEAKERS}，收到：{n}")
    return n


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(DEFAULT_LANGUAGE),
) -> dict:
    _validate_language(language)
    audio_bytes = await _read_and_validate_upload(file)
    try:
        transcript, resolved = get_diarized_transcript(
            audio_bytes, language=language, filename=file.filename or "audio.m4a",
        )
    except Exception as exc:
        raise HTTPException(500, f"轉錄失敗：{exc}") from exc
    prompt = build_correction_prompt(transcript, language=resolved)
    return {"transcript": transcript, "prompt": prompt, "detected_language": resolved}


@app.post("/transcribe-voice")
async def transcribe_voice(
    file: UploadFile = File(...),
    language: str = Form(DEFAULT_LANGUAGE),
) -> dict:
    _validate_language(language)
    audio_bytes = await _read_and_validate_upload(file)
    try:
        transcript, resolved = get_plain_transcript(
            audio_bytes, language=language, filename=file.filename or "audio.webm",
        )
    except Exception as exc:
        raise HTTPException(500, f"轉錄失敗：{exc}") from exc
    return {"transcript": transcript, "detected_language": resolved}


@app.post("/transcribe-meeting")
async def transcribe_meeting(
    file: UploadFile = File(...),
    language: str = Form(DEFAULT_LANGUAGE),
    num_speakers: Optional[str] = Form(None),
) -> dict:
    _validate_language(language)
    speakers = _parse_num_speakers(num_speakers)
    audio_bytes = await _read_and_validate_upload(file)
    try:
        transcript, resolved = get_diarized_transcript(
            audio_bytes, language=language, num_speakers=speakers,
            filename=file.filename or "audio.m4a",
        )
    except Exception as exc:
        raise HTTPException(500, f"轉錄失敗：{exc}") from exc
    prompt = build_correction_prompt(transcript, language=resolved)
    return {"transcript": transcript, "prompt": prompt, "detected_language": resolved}
