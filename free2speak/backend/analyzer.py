"""Shared Claude analysis logic — used by both `/upload` in main.py and
by `reanalyze.py` for prompt A/B on existing transcripts.

Sonnet 4.6 occasionally mangles tool-use output by JSON-stringifying the
`additions`/`graduations` arrays (observed 2026-07-05 on transcripts where
several `you_said` fields contain nested `"..."` quotes for error
highlighting). We can't fully prevent it, so we retry with a different
temperature, and finally escalate to Opus 4.7 which handles nested tool-use
more reliably.
"""
import json
import os

from opus_client import emit_tool as opus_emit_tool
from prompts.claude_analyze import TOOL as ANALYZE_TOOL, build as build_analyze_prompt

ANALYZE_MODEL = os.environ.get("ANTHROPIC_ANALYZE_MODEL", "claude-sonnet-4-6")


def _normalize(result: dict) -> tuple[bool, bool]:
    """In-place: coerce string-shaped additions/graduations back to list if
    possible. Return (additions_ok, graduations_ok) so the caller can decide
    whether to retry."""
    ok = {"additions": False, "graduations": False}
    for field in ("additions", "graduations"):
        v = result.get(field)
        if isinstance(v, list):
            ok[field] = True
            continue
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    result[field] = parsed
                    ok[field] = True
                    print(f"[analyze] recovered stringified {field}", flush=True)
            except json.JSONDecodeError as e:
                print(f"[analyze] {field} came back as unparseable string ({e})",
                      flush=True)
    return ok["additions"], ok["graduations"]


def analyze(transcript: str, active_errors: list) -> dict:
    """Run the analysis with retry + Opus fallback. Guarantees the returned
    dict has `additions` and `graduations` as lists (possibly empty if all
    attempts failed)."""
    prompt = build_analyze_prompt(transcript, active_errors)
    result: dict = {}
    attempts = [
        (ANALYZE_MODEL, 0.2, "sonnet@0.2"),
        (ANALYZE_MODEL, 0.5, "sonnet@0.5"),
        ("claude-opus-4-7", None, "opus"),
    ]
    for i, (model, temp, label) in enumerate(attempts, 1):
        print(f"[analyze] attempt {i}/{len(attempts)}: {label}", flush=True)
        result = opus_emit_tool(
            prompt, ANALYZE_TOOL, model=model, temperature=temp, max_tokens=8192)
        a_ok, g_ok = _normalize(result)
        if a_ok and g_ok:
            return result
        print(f"[analyze] {label} malformed (additions_ok={a_ok}, "
              f"graduations_ok={g_ok}), retrying", flush=True)

    print("[analyze] WARN: all attempts failed; empty fallbacks", flush=True)
    for field in ("additions", "graduations"):
        if not isinstance(result.get(field), list):
            result[field] = []
    return result
