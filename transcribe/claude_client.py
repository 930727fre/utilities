"""Anthropic Messages API client — sync-only thin HTTP wrapper.

Same shape as gemini_client.py: exposes `generate_json(prompt, response_schema)`.
Uses tool-use with a forced tool call for reliable JSON output.

Schemas passed in can be any JSON-Schema shape (object, array, primitive).
Top-level non-object schemas get wrapped in {"result": <schema>} since
Anthropic tool input_schema requires object root, then unwrapped on return.
"""
import json
import os
import time
from typing import Any, Optional

API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192

MAX_RETRIES = 3
RETRY_BACKOFF_SEC = (1.0, 3.0, 8.0)

_TOOL_NAME = "respond"


def _api_key() -> str:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} not set in environment")
    return key


def _wrap_schema(response_schema: dict) -> tuple[dict, bool]:
    """Anthropic requires tool input_schema to be an object. Wrap if it isn't."""
    if response_schema.get("type") == "object":
        return response_schema, False
    return (
        {
            "type": "object",
            "properties": {"result": response_schema},
            "required": ["result"],
        },
        True,
    )


def _extract_tool_input(resp_json: dict) -> dict:
    for block in resp_json.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == _TOOL_NAME:
            return block.get("input") or {}
    raise RuntimeError(f"No tool_use block in response: {resp_json}")


def generate_json(prompt: str, response_schema: dict, *,
                  temperature: float = 0.2, model: Optional[str] = None,
                  max_tokens: int = DEFAULT_MAX_TOKENS,
                  timeout: tuple = (10, 180)) -> Any:
    import requests

    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    schema, unwrap = _wrap_schema(response_schema)

    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "name": _TOOL_NAME,
            "description": "Return the structured response.",
            "input_schema": schema,
        }],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(API_URL, json=body, headers=headers, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                last_exc = RuntimeError(f"Anthropic {r.status_code}: {r.text[:200]}")
                time.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])
                continue
            r.raise_for_status()
            tool_input = _extract_tool_input(r.json())
            return tool_input["result"] if unwrap else tool_input
        except requests.RequestException as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])

    raise RuntimeError(f"Anthropic call failed after {MAX_RETRIES} attempts: {last_exc}")
