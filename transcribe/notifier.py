"""Telegram push notifications for pipeline lifecycle events.

Phase 1: pure text notifications, no interactive buttons. FastAPI (this
container) → Telegram Bot API → user's chat. No separate bot process,
no long-polling — outbound HTTP only.

Fires on:
  - `notify_success`: canonical SRT successfully produced (via any tier
    — bundled / OS / whisper fallback / archive attach)
  - `notify_failure`: `.whisper-failed` / `.whisper-polluted` /
    `.annotate-failed` stamped (i.e. pipeline gave up and needs the
    user to intervene)

Config (both required, both silent-disable on absence):
  TELEGRAM_BOT_TOKEN  — from @BotFather (Telegram, /newbot flow)
  TELEGRAM_CHAT_ID    — the numeric chat_id of your DM with the bot

Getting chat_id after creating the bot:
  1. Send any message to your bot in Telegram
  2. curl "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq
  3. Copy result.message.chat.id (an integer, may be negative)

Failure handling: any HTTP / config error is caught and logged. The
notifier NEVER raises — pipeline correctness takes priority over
notification delivery.
"""
import os
from typing import Optional

import httpx

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
_API_TIMEOUT = 5.0  # seconds — short since we don't want to slow pipeline


def _enabled() -> bool:
    return bool(_BOT_TOKEN and _CHAT_ID)


def _send(text: str) -> None:
    """Fire-and-forget send. Best-effort HTTP POST to Telegram; failure
    is logged and swallowed."""
    if not _enabled():
        return
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={
                "chat_id": _CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=_API_TIMEOUT,
        )
        if r.status_code >= 400:
            print(f"[notifier] send returned {r.status_code}: {r.text[:200]}", flush=True)
    except httpx.HTTPError as exc:
        print(f"[notifier] send failed: {exc}", flush=True)


def _fmt_elapsed(sec: Optional[float]) -> str:
    if sec is None or sec <= 0:
        return ""
    m, s = divmod(int(sec), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f" | {h}:{m:02d}:{s:02d}"
    return f" | {m}:{s:02d}"


def _escape_html(s: str) -> str:
    """Minimal HTML escape for text we drop inside <b>…</b>. Telegram's
    HTML parse mode requires escaping &, <, > in body text."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify_success(video_name: str, tier: str, elapsed_sec: Optional[float] = None) -> None:
    """Called when a canonical SRT lands. `tier` names how the pipeline
    got there (e.g. 'archive', 'bundled', 'opensubtitles-text',
    'whisper'). `elapsed_sec` is optional wall-clock duration since the
    pipeline started on this file."""
    text = f"✅ <b>{_escape_html(video_name)}</b>\ntier: {_escape_html(tier)}{_fmt_elapsed(elapsed_sec)}"
    _send(text)


def notify_failure(video_name: str, kind: str, reason: str) -> None:
    """Called when the pipeline stamps a failure sidecar and gives up.
    `kind` is the failure category (short label — 'whisper failed',
    'whisper polluted', 'annotate failed'). `reason` is the free-text
    detail written into the sidecar."""
    text = (
        f"❌ <b>{_escape_html(video_name)}</b>\n"
        f"{_escape_html(kind)}: {_escape_html(reason)}"
    )
    _send(text)
