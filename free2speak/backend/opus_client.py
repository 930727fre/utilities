"""Anthropic Opus client — thin wrapper around the `anthropic` SDK.

Used by the roleplay and drill generators. Tool-use is the recommended path
for structured output: we define a tool whose input_schema matches the desired
response shape, ask the model to call it, and read tool_use.input as JSON.

Cost expectations (Opus 4.7, $2/M in $12/M out):
- roleplay generation: ~$0.02 per call
- drill generation:    ~$0.03 per call
- ~$0.05/day if user runs both → ~$1.50/month
"""
import os
from typing import Any

from anthropic import Anthropic
from fastapi import HTTPException

DEFAULT_MODEL = os.environ.get("OPUS_MODEL", "claude-opus-4-7")
DEFAULT_MAX_TOKENS = 4096


def _client() -> Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=api_key)


def emit_tool(prompt: str, tool: dict, *, model: str | None = None,
              max_tokens: int = DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """Send `prompt`, force the model to invoke `tool`, return tool_use.input.

    Tool-use is Anthropic's structured-output pattern. We pin tool_choice to
    require the named tool, so the model can't return prose by accident.

    Note: `temperature` is intentionally omitted — Claude Opus 4.7 deprecated it
    in favor of internal extended-thinking-style sampling.
    """
    try:
        resp = _client().messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Opus call failed: {e}")

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
            return block.input

    raise HTTPException(
        status_code=502,
        detail=f"Opus didn't invoke the {tool['name']} tool: {resp.content!r}",
    )
