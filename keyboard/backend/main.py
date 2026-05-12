import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma3:12b")
CORRECTIONS_PATH = Path(os.environ.get("CORRECTIONS_PATH", "/app/data/corrections.json"))
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", "/app/frontend"))

DEFAULT_PROMPT = (
    "Voice memo from Taiwan. Speaker may use English only, Traditional Chinese only, "
    "or freely mix both. Examples:\n"
    "- 我用 Docker 部署到 Kubernetes，再 git push 到 main。\n"
    "- Open VS Code and check the API response.\n"
    "- I think 這個 design 不太對，我們重新討論一下。\n"
    "Keep technical terms (Docker, Kubernetes, API, VS Code, git, deploy, merge) in "
    "English. For Chinese, use Traditional Chinese (台灣繁體)."
)

LLM_PROMPT_TEMPLATE = """{vocab_rule}You are cleaning up a voice transcript with the lightest possible touch.

Hard rules:
- Keep the original language(s). Never translate. If input is English, output English. If input is Chinese, output Chinese. If mixed (very common — Taiwanese speakers naturally code-switch), keep the mix exactly as spoken. Example: "我用 Docker 部署到 Kubernetes" stays as "我用 Docker 部署到 Kubernetes" — do not translate "Docker"/"Kubernetes" into Chinese, do not translate the Chinese verbs into English.
- For Chinese output, use Traditional Chinese (Taiwan / 台灣繁體).
- Fix only obvious typos and homophone errors. Add basic punctuation. Remove filler words (uh, um, 嗯, 呃, 那個).
- Do not add anything not in the original — no parenthetical translations, no romanizations, no explanations, no clarifications.
- If a word is unclear, leave it as the transcript has it. Do not guess.

Output only the cleaned transcript. No preamble, no commentary.

Transcript:
{text}

Cleaned:"""


def build_vocab_rule(corrections: dict[str, list[str]]) -> str:
    if not corrections:
        return ""
    lines = [
        "=== MANDATORY VOCABULARY OVERRIDES ===",
        "These rewrites take priority over your prior knowledge. Even if the right-hand form is a real product/brand name you recognize, you MUST rewrite it to the left-hand form. Match case-insensitively, with or without surrounding punctuation:",
        "",
    ]
    for correct, wrongs in corrections.items():
        wrongs_clean = [w for w in (wrongs or []) if w]
        if wrongs_clean:
            lines.append(f"  - rewrite {', '.join(repr(w) for w in wrongs_clean)} → {correct!r}")
        else:
            lines.append(f"  - canonical form: {correct!r}")
    lines.append("=== END VOCABULARY OVERRIDES ===")
    lines.append("")
    return "\n".join(lines) + "\n"

_corrections: dict[str, list[str]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_corrections()
    yield


app = FastAPI(lifespan=lifespan)


def load_corrections() -> dict[str, list[str]]:
    global _corrections
    if CORRECTIONS_PATH.exists():
        with CORRECTIONS_PATH.open("r", encoding="utf-8") as f:
            _corrections = json.load(f)
    else:
        _corrections = {}
    return _corrections


def save_corrections(data: dict[str, list[str]]) -> None:
    tmp = CORRECTIONS_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CORRECTIONS_PATH)
    load_corrections()


async def call_whisper(audio_bytes: bytes, filename: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=300.0) as client:
        files = {"file": (filename, audio_bytes, "audio/webm")}
        data = {
            "model": "whisper-1",
            "prompt": DEFAULT_PROMPT,
            "temperature": "0",
            "response_format": "json",
        }
        r = await client.post(
            f"{WHISPER_URL}/v1/audio/transcriptions",
            files=files,
            data=data,
        )
        r.raise_for_status()
        return r.json()


async def call_ollama_correction(text: str) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        prompt = LLM_PROMPT_TEMPLATE.format(
            text=text,
            vocab_rule=build_vocab_rule(_corrections),
        )
        payload = {
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 200},
        }
        r = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "").strip()


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile):
    audio_bytes = await audio.read()

    t0 = time.perf_counter()
    whisper_result = await call_whisper(audio_bytes, audio.filename or "audio.webm")
    raw_text = (whisper_result.get("text") or "").strip()
    t1 = time.perf_counter()

    final_text = await call_ollama_correction(raw_text) if raw_text else ""
    t2 = time.perf_counter()

    return {
        "raw": raw_text,
        "final": final_text,
        "timing": {
            "whisper_ms": int((t1 - t0) * 1000),
            "llm_ms": int((t2 - t1) * 1000),
        },
    }


@app.get("/api/corrections")
async def get_corrections():
    return load_corrections()


@app.put("/api/corrections")
async def put_corrections(payload: dict[str, list[str]]):
    save_corrections(payload)
    return {"ok": True}


@app.middleware("http")
async def no_cache_html(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = FRONTEND_DIR / "index.html"
    html = html_path.read_text(encoding="utf-8")
    for asset in ("style.css", "app.js"):
        v = int((FRONTEND_DIR / asset).stat().st_mtime)
        html = html.replace(f"/{asset}", f"/{asset}?v={v}")
    return HTMLResponse(html)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
