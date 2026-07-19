"""LLM usage accounting — per-caller token counter with daily digest.

Every LLM callsite in transcribe passes `caller="..."` (and optionally
`target="..."`) to its client wrapper. The client extracts usage from
the API response and hands it here via `record()`. Once a day, on the
first reconciler tick past 4am TPE, `maybe_fire_daily_report()` sums
the log, sends a per-caller digest to Telegram, and rotates the log so
tomorrow starts clean.

Purpose: catch runaway token spend (filter loop, retry cycle) without
having to guess thresholds. You get a daily total per caller; if
something's off, the number is off. If everything's fine, the digest
matches your mental baseline and you scroll past it.

Rotation is rename-then-read-then-delete. Rename is atomic on ext4, so
any concurrent LLM call after the rename lands in a fresh log — no
lock needed, no race between rotate and record.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _TPE: Optional[object] = ZoneInfo("Asia/Taipei")
except ImportError:
    _TPE = None

DATA_ROOT = Path(os.environ.get("TRANSCRIBE_DATA_ROOT", "/app/data"))
LOG_PATH = DATA_ROOT / "llm_calls.log"
STATE_PATH = DATA_ROOT / "llm_report_state.json"
REPORT_HOUR = 4   # fire at first tick past 4am TPE

# USD per 1M tokens (input, output). Snapshot as of 2026-07; update when
# vendor rates change. Unknown models default to (0, 0) so a new model
# spending unnoticed doesn't crash the digest, but its cost silently
# shows $0.00 — a "why is annotate $0?" moment is the signal to add it
# here. Cached input is billed lower by Anthropic but we don't use
# prompt caching, so the plain input rate is what applies.
_PRICING = {
    "claude-opus-4-7":       (15.00, 75.00),
    "claude-sonnet-4-6":     ( 3.00, 15.00),
    "claude-haiku-4-5":      ( 1.00,  5.00),
    "gemini-3.1-flash-lite": ( 0.25,  1.50),
    "gemini-3.1-flash":      ( 0.30,  2.50),
    "gemini-3.1-pro":        ( 1.25, 10.00),
}


def _cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    in_price, out_price = _PRICING.get(model, (0.0, 0.0))
    return (in_tok * in_price + out_tok * out_price) / 1_000_000

_lock = threading.Lock()


def record(*, model: str, caller: str, target: str,
           input_tokens: int, output_tokens: int) -> None:
    """Append one call to the log. Called by the LLM client wrappers
    after a successful API response. All errors swallowed — accounting
    must never break the pipeline."""
    if not caller:
        return
    try:
        ts = datetime.now(_TPE).isoformat() if _TPE else datetime.utcnow().isoformat() + "Z"
        # Defensive sanitize — tab / newline in target would break the TSV.
        target = str(target).replace("\t", " ").replace("\n", " ")
        line = f"{ts}\t{model}\t{caller}\t{target}\t{input_tokens}\t{output_tokens}\n"
        with _lock:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def maybe_fire_daily_report() -> None:
    """Called every reconciler tick. Cheap no-op except once per day,
    on the first tick past 4am TPE."""
    if _TPE is None:
        return   # Python < 3.9; time-zoned "4am TPE" isn't reliable
    now = datetime.now(_TPE)
    if now.hour < REPORT_HOUR:
        return
    today = now.date().isoformat()
    state = _load_state()
    if state.get("last_fired") == today:
        return
    try:
        _fire_report(today)
    except Exception as exc:
        print(f"[llm-usage] daily report failed: {exc}", flush=True)
        return
    state["last_fired"] = today
    _save_state(state)


def _fire_report(day: str) -> None:
    # Rotate: atomic rename so any in-flight LLM call lands in the fresh
    # log rather than the rotated snapshot we're about to process.
    if not LOG_PATH.is_file():
        _send_digest(day, {})
        return
    rotated = LOG_PATH.with_suffix(f".rotated.{day}")
    with _lock:
        LOG_PATH.rename(rotated)

    # Aggregate by (caller, model). A single caller can span models —
    # annotate falls back Sonnet → Gemini on content refusal, so we need
    # separate rows to price each correctly and to make the fallback
    # frequency visible in the digest.
    counts: dict[tuple[str, str], dict] = {}
    try:
        text = rotated.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        _, model, caller, _target, in_tok, out_tok = parts
        entry = counts.setdefault((caller, model), {"calls": 0, "in": 0, "out": 0})
        entry["calls"] += 1
        try:
            entry["in"] += int(in_tok)
            entry["out"] += int(out_tok)
        except ValueError:
            pass

    _send_digest(day, counts)
    try:
        rotated.unlink()
    except OSError:
        pass


def _send_digest(day: str, counts: dict) -> None:
    lines = [f"LLM report — {day} (past 24h)", ""]
    if not counts:
        lines.append("(no calls)")
    else:
        # Sort by USD descending — expensive callers surface first.
        rows = []
        total_usd = 0.0
        for (caller, model), s in counts.items():
            usd = _cost_usd(model, s["in"], s["out"])
            rows.append((caller, model, s["calls"], s["in"], s["out"], usd))
            total_usd += usd
        rows.sort(key=lambda r: r[5], reverse=True)
        for caller, model, calls, in_tok, out_tok, usd in rows:
            in_k = in_tok / 1000
            out_k = out_tok / 1000
            lines.append(
                f"{caller} ({model}): {calls} calls, "
                f"{in_k:.1f}k in / {out_k:.1f}k out, ${usd:.2f}"
            )
        lines.append("")
        lines.append(f"Total: ${total_usd:.2f}")
    msg = "\n".join(lines)
    try:
        from notifier import _send
        _send(msg)
    except Exception as exc:
        print(f"[llm-usage] Telegram send failed ({exc}); digest was:\n{msg}", flush=True)
