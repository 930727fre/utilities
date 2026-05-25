"""Gemini API client — thin wrapper used by xyt / flashcard / keyboard.

Replaces our ollama calls. Two reasons we wrote it instead of using the official
SDK: (1) it's 50 lines of straightforward HTTP, (2) sync vs async needs picks
its own HTTP lib (`requests` for xyt/flashcard, `httpx` for keyboard's async
handler), and the SDK pulls in extra deps we don't need.

Designed for the free tier: short retry-with-backoff on 429 (rate limit) and
5xx (server hiccup), no retry on 4xx that aren't 429.
"""
import json
import os
import time
from typing import Any, Optional

API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-2.5-flash-lite"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Retry behavior: short, bounded. Real outages should surface, not be papered over.
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = (1.0, 3.0, 8.0)  # one per attempt


def _api_key() -> str:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} not set in environment")
    return key


def _build_request(prompt: str, response_schema: Optional[dict] = None,
                   temperature: float = 0.2) -> dict:
    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    if response_schema is not None:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = response_schema
    return body


def _extract_text(resp_json: dict) -> str:
    """Pull the model's text out of Gemini's nested response shape."""
    candidates = resp_json.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"no candidates in response: {resp_json}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        raise RuntimeError(f"no parts in candidate: {candidates[0]}")
    return parts[0].get("text", "").strip()


def generate(prompt: str, *, response_schema: Optional[dict] = None,
             temperature: float = 0.2, model: Optional[str] = None,
             timeout: tuple = (10, 60), session: Optional[Any] = None) -> str:
    """Send a single Gemini request. Returns the raw text (JSON string if schema set).

    `session` lets callers reuse a connection across many calls (xyt batching).
    """
    import requests  # lazy: async-only callers (keyboard) don't need requests installed

    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    url = f"{BASE_URL}/{model}:generateContent?key={_api_key()}"
    body = _build_request(prompt, response_schema, temperature)
    s = session or requests
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_RETRIES):
        try:
            r = s.post(url, json=body, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                last_exc = RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
                time.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])
                continue
            r.raise_for_status()
            return _extract_text(r.json())
        except requests.RequestException as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])

    raise RuntimeError(f"Gemini call failed after {MAX_RETRIES} attempts: {last_exc}")


def generate_json(prompt: str, response_schema: dict, **kwargs) -> Any:
    """Convenience: call with a schema and parse the JSON response."""
    raw = generate(prompt, response_schema=response_schema, **kwargs)
    return json.loads(raw)


# ── Async variant (for keyboard's async handlers) ────────────────────────────

async def generate_async(prompt: str, *, response_schema: Optional[dict] = None,
                         temperature: float = 0.2, model: Optional[str] = None,
                         timeout: float = 60.0) -> str:
    import httpx  # local import — only async callers pay for the dep

    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    url = f"{BASE_URL}/{model}:generateContent?key={_api_key()}"
    body = _build_request(prompt, response_schema, temperature)
    last_exc: Optional[Exception] = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(MAX_RETRIES):
            try:
                r = await client.post(url, json=body)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    last_exc = RuntimeError(f"Gemini {r.status_code}: {r.text[:200]}")
                    import asyncio
                    await asyncio.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])
                    continue
                r.raise_for_status()
                return _extract_text(r.json())
            except httpx.HTTPError as e:
                last_exc = e
                import asyncio
                await asyncio.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])

    raise RuntimeError(f"Gemini call failed after {MAX_RETRIES} attempts: {last_exc}")


async def generate_json_async(prompt: str, response_schema: dict, **kwargs) -> Any:
    raw = await generate_async(prompt, response_schema=response_schema, **kwargs)
    return json.loads(raw)
