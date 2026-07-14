"""Telegram push notifications for pipeline lifecycle events.

Wrapper-level aggregation, stateless. Every pipeline event (success or
failure) triggers a filesystem check — is the wrapper fully terminal?
Only the last event in a wrapper sees "all done" and fires a summary.
Failures don't get individual push notifications; they roll up into the
same wrapper summary alongside successes.

Design invariants:
  - No in-memory bookkeeping — pure derivation from filesystem.
  - `.filtered` sentinel is the authoritative canonical list per wrapper.
  - Canonical `.srt` existence + failure sidecars are the completion signals.
  - Same event may re-fire summary if wrapper's state changed (e.g., user
    retry succeeds → wrapper terminal with 9 ✅ / 1 ❌ → later retry succeeds
    → wrapper terminal with 10 ✅ / 0 ❌ → new summary fires).

Concurrency: the English pipeline runs on a single-worker executor so
per-file events are naturally serialised — only the final event sees
all-terminal. The Chinese translator has 2 workers; in the extreme case
both may see all-terminal simultaneously and double-fire. Accepted as a
rare no-op; duplicate message is harmless.

Config (both required, both silent-disable on absence):
  TELEGRAM_BOT_TOKEN  — from @BotFather (/newbot flow)
  TELEGRAM_CHAT_ID    — numeric chat_id of your DM with the bot

Failure handling: any HTTP / config error is caught and logged. The
notifier NEVER raises — pipeline correctness takes priority over
notification delivery.
"""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from srt_source import (
    annotate_failed_path,
    whisper_failed_path,
    whisper_polluted_path,
)

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Taipei")
except Exception:
    _TZ = timezone.utc

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
_API_TIMEOUT = 5.0  # short; we don't want to slow the pipeline


def _enabled() -> bool:
    return bool(_BOT_TOKEN and _CHAT_ID)


def _send(text: str) -> None:
    """Fire-and-forget HTTP POST to Telegram. Best-effort — failure
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
                "disable_web_page_preview": True,
            },
            timeout=_API_TIMEOUT,
        )
        if r.status_code >= 400:
            print(f"[notifier] send returned {r.status_code}: {r.text[:200]}", flush=True)
    except httpx.HTTPError as exc:
        print(f"[notifier] send failed: {exc}", flush=True)


def _now_stamp() -> str:
    """Formatted timestamp aligned with utilities/backup — Asia/Taipei
    timezone, ISO-ish `YYYY-MM-DD HH:MM:SS` (no timezone suffix)."""
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _summarize_wrapper_title(wrapper_name: str) -> str:
    """Derive a clean human title from the wrapper's canonical outputs
    instead of showing the raw torrent directory name (which is often
    `Show.Name.S01.Complete.1080p.WEB-DL.CODEC-GROUP` — noisy).

    Reads `.filtered` sentinel, parses each canonical path shape:
      Movies/<title>/<file>              → "<title>"
      TV/<show>/Season NN/<file>         → "<show> S01"  (single season)
                                         → "<show> S01-S08"  (range)

    Falls back to the raw wrapper name if parse fails."""
    from bt_filter import ARTIFACT_ROOT, load_manifest
    canonical_videos = load_manifest(wrapper_name)
    if not canonical_videos:
        return wrapper_name
    shows: set[str] = set()
    seasons_by_show: dict[str, set[str]] = {}
    for v in canonical_videos:
        try:
            parts = v.resolve().relative_to(ARTIFACT_ROOT.resolve()).parts
        except ValueError:
            continue
        if len(parts) < 2 or parts[0] not in ("Movies", "TV"):
            continue
        show = parts[1]
        shows.add(show)
        if parts[0] == "TV" and len(parts) >= 3:
            seasons_by_show.setdefault(show, set()).add(parts[2])
    if not shows:
        return wrapper_name
    if len(shows) > 1:
        first = sorted(shows)[0]
        return f"{first} +{len(shows) - 1} more"
    show = next(iter(shows))
    seasons = sorted(seasons_by_show.get(show, set()))
    if not seasons:
        return show                          # movie
    def _short(s: str) -> str:
        return s.replace("Season ", "S")
    if len(seasons) == 1:
        return f"{show} {_short(seasons[0])}"
    return f"{show} {_short(seasons[0])}-{_short(seasons[-1])}"


# ── wrapper lookup + state derivation ──────────────────────────────────

def _find_wrapper_for_video(video: Path) -> Optional[str]:
    """Return the bt wrapper name whose `.filtered` sentinel lists this
    video's canonical path, or None if the video isn't tracked by any
    sentinel (e.g. YouTube pipeline, manual placement).

    Cheap linear scan over `_processed/*.filtered` — sentinel files are
    small and there are only a few dozen at most for a typical library."""
    from bt_filter import ARTIFACT_ROOT, PROCESSED_DIR
    try:
        rel_str = str(video.resolve().relative_to(ARTIFACT_ROOT.resolve()))
    except ValueError:
        return None
    if not PROCESSED_DIR.is_dir():
        return None
    for sentinel in PROCESSED_DIR.glob("*.filtered"):
        try:
            text = sentinel.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rel_str in text.splitlines():
            return sentinel.stem
    return None


def _wrapper_terminal_state_zh(wrapper_name: str) -> Optional[dict]:
    """Return {total, translated_paths, failed_paths, all_terminal} for
    Chinese translation of a wrapper. Denominator = canonicals that have
    an English `.srt` (only translatable ones). Each candidate is either
    translated (`.zh-tw.srt` exists), failed (`.zh-tw.srt.error` exists),
    or pending. Returns None if the sentinel isn't readable."""
    from bt_filter import load_manifest
    canonical_videos = load_manifest(wrapper_name)
    if not canonical_videos:
        return None
    translatable = [v for v in canonical_videos if v.with_suffix(".srt").exists()]
    if not translatable:
        return None
    translated: list[Path] = []
    failed: list[tuple[Path, str]] = []  # (video, reason)
    pending: list[Path] = []
    for video in translatable:
        zh = video.parent / f"{video.stem}.zh-tw.srt"
        err = Path(str(zh) + ".error")
        if zh.exists():
            translated.append(video)
        elif err.exists():
            try:
                reason = err.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                reason = "(error sidecar unreadable)"
            failed.append((video, reason))
        else:
            pending.append(video)
    return {
        "total": len(translatable),
        "translated": translated,
        "failed": failed,
        "pending": pending,
        "all_terminal": not pending,
    }


def _wrapper_terminal_state(wrapper_name: str) -> Optional[dict]:
    """Return {total, succeeded_paths, failed_paths, all_terminal} for a
    wrapper, computed by walking its canonical file list and checking
    each for `.srt` (success) or any of the three failure sidecars.

    Returns None if the sentinel can't be read (deleted between event
    and check, treat as no-op)."""
    from bt_filter import load_manifest
    canonical_videos = load_manifest(wrapper_name)
    if not canonical_videos:
        return None
    succeeded: list[Path] = []
    failed: list[tuple[Path, str, str]] = []  # (video, kind, reason)
    for video in canonical_videos:
        srt = video.with_suffix(".srt")
        if srt.exists():
            succeeded.append(video)
            continue
        for kind, path_fn in (
            ("whisper failed", whisper_failed_path),
            ("whisper polluted", whisper_polluted_path),
            ("annotate failed", annotate_failed_path),
        ):
            sidecar = path_fn(video)
            if sidecar.exists():
                try:
                    reason = sidecar.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    reason = "(sidecar unreadable)"
                failed.append((video, kind, reason))
                break
    return {
        "total": len(canonical_videos),
        "succeeded": succeeded,
        "failed": failed,
        "all_terminal": len(succeeded) + len(failed) == len(canonical_videos),
    }


# ── summary rendering ─────────────────────────────────────────────────

def _fmt_summary(wrapper_name: str, state: dict) -> str:
    ok = len(state["succeeded"])
    bad = len(state["failed"])
    total = state["total"]
    title = _summarize_wrapper_title(wrapper_name)
    ts = _now_stamp()
    verb = "done" if bad == 0 else "partial"
    line1 = f"Transcribe {verb} {ts} | {title} | {ok}/{total} ok"
    if bad:
        line1 += f" | {bad}/{total} failed"
    if bad == 0:
        return line1
    failed_bits = []
    for video, kind, reason in state["failed"][:10]:
        failed_bits.append(f"{video.name} ({kind})")
    tail = "Failed: " + ", ".join(failed_bits)
    if bad > 10:
        tail += f", …+{bad - 10}"
    return line1 + "\n" + tail


def _fmt_summary_zh(wrapper_name: str, state: dict) -> str:
    ok = len(state["translated"])
    bad = len(state["failed"])
    total = state["total"]
    title = _summarize_wrapper_title(wrapper_name)
    ts = _now_stamp()
    verb = "done" if bad == 0 else "partial"
    line1 = f"Transcribe zh {verb} {ts} | {title} | {ok}/{total} ok"
    if bad:
        line1 += f" | {bad}/{total} failed"
    if bad == 0:
        return line1
    failed_bits = [v.name for v, _ in state["failed"][:10]]
    tail = "Failed: " + ", ".join(failed_bits)
    if bad > 10:
        tail += f", …+{bad - 10}"
    return line1 + "\n" + tail


def _fmt_individual_success(video: Path, tier: str) -> str:
    return f"Transcribe done {_now_stamp()} | {video.name} | tier={tier}"


def _fmt_individual_failure(video: Path, kind: str, reason: str) -> str:
    short = reason.replace("\n", " ")[:120]
    return f"Transcribe failed {_now_stamp()} | {video.name} | {kind}: {short}"


# ── public API ─────────────────────────────────────────────────────────

def maybe_fire_wrapper_summary(wrapper_name: str) -> None:
    """Check the given wrapper's terminal state; if all its canonical
    files have either a `.srt` or a failure sidecar, fire a Telegram
    summary. Idempotent per call — safe to call from multiple pipeline
    events (only the "last-event" call will see `all_terminal` True)."""
    state = _wrapper_terminal_state(wrapper_name)
    if state is None or not state["all_terminal"]:
        return
    _send(_fmt_summary(wrapper_name, state))


def notify_success(video: Path, tier: str) -> None:
    """Called by the pipeline when a canonical SRT is produced. Routes
    to wrapper-level summary if the video is part of a tracked bt
    wrapper, otherwise falls back to an individual notification
    (YouTube canonicals, manual placements)."""
    wrapper = _find_wrapper_for_video(video)
    if wrapper is None:
        _send(_fmt_individual_success(video, tier))
        return
    maybe_fire_wrapper_summary(wrapper)


def notify_failure(video: Path, kind: str, reason: str) -> None:
    """Called by the pipeline when a failure sidecar is stamped. Same
    routing as `notify_success`: bt wrapper → wrapper summary (once all
    siblings are terminal); non-wrapper canonical → individual push."""
    wrapper = _find_wrapper_for_video(video)
    if wrapper is None:
        _send(_fmt_individual_failure(video, kind, reason))
        return
    maybe_fire_wrapper_summary(wrapper)


def maybe_fire_wrapper_summary_zh(wrapper_name: str) -> None:
    """Chinese-side twin of `maybe_fire_wrapper_summary`. Fires when
    every translatable file in the wrapper (i.e. every canonical with
    an English `.srt`) has either a `.zh-tw.srt` or a `.zh-tw.srt.error`
    sibling."""
    state = _wrapper_terminal_state_zh(wrapper_name)
    if state is None or not state["all_terminal"]:
        return
    _send(_fmt_summary_zh(wrapper_name, state))


def notify_zh_success(video: Path) -> None:
    """Called by the translator on successful zh-tw sibling write."""
    wrapper = _find_wrapper_for_video(video)
    if wrapper is None:
        # Non-wrapper canonical (YT probably, though YT typically
        # doesn't get translated). Individual notification.
        _send(f"Transcribe zh done {_now_stamp()} | {video.name}")
        return
    maybe_fire_wrapper_summary_zh(wrapper)


def notify_zh_failure(video: Path, reason: str) -> None:
    """Called by the translator when translation fails and an .error
    sidecar is stamped."""
    wrapper = _find_wrapper_for_video(video)
    if wrapper is None:
        short = reason.replace("\n", " ")[:120]
        _send(f"Transcribe zh failed {_now_stamp()} | {video.name} | {short}")
        return
    maybe_fire_wrapper_summary_zh(wrapper)
