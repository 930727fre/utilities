"""Telegram push notifications for pipeline lifecycle events.

Season/movie-level aggregation, stateless. Every pipeline event
(success or failure) triggers a filesystem check — is the video's
"group" fully terminal? Only the last event in a group sees "all done"
and fires a summary.

Grouping (semantic media unit, not the raw torrent wrapper):
  Movies/<title>/*.mkv               → group = "<title>"
  TV/<show>/Season NN/*.mkv          → group = "<show> S NN"

For a single-season torrent pack the group == wrapper — no change in
behavior. For a complete-series pack (e.g. `GoT S01-S08`), one summary
fires per season as each season completes, instead of a single silent
18-hour wait for the whole pack.

Design invariants:
  - No in-memory bookkeeping — pure derivation from filesystem.
  - Group = the parent directory the canonical video lives in. Its
    sibling `.mkv/.mp4/…` files are the group members.
  - `.srt` existence + failure sidecars are the completion signals.

Concurrency: English pipeline is single-worker → serial events → no
race. Chinese translator has 2 workers; extreme corner case can
double-fire — accepted as harmless.

Config (both required at compose parse time):
  TELEGRAM_BOT_TOKEN  — from @BotFather (/newbot flow)
  TELEGRAM_CHAT_ID    — numeric chat_id of your DM with the bot

Failure handling: any HTTP / config error is caught and logged. The
notifier NEVER raises — pipeline correctness takes priority over
notification delivery.
"""
import os
from pathlib import Path
from typing import Optional

import httpx

from srt_source import any_failure_sidecar_with_kind

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
_API_TIMEOUT = 5.0

# Video extension set for identifying canonical siblings inside a group
# directory. Kept in sync with bt_filter.VIDEO_EXTS but duplicated here
# to keep notifier decoupled from bt_filter's internals.
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}


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


# ── group lookup + state derivation ────────────────────────────────────

def _find_group_for_video(video: Path) -> Optional[tuple[Path, str]]:
    """Return `(group_dir, group_label)` for a canonical video, or None
    if the path shape isn't a recognized bt canonical layout (YouTube
    dumps, staging paths — those get individual notifications).

    Group_dir is the directory whose sibling videos define the aggregation
    set. Group_label is what shows up in the Telegram message (clean
    canonical title, no release-group junk).

      /artifact/Movies/<title>/<file>.mkv
        → (<title>-dir, "<title>")
      /artifact/TV/<show>/Season NN/<file>.mkv
        → (<Season NN>-dir, "<show> SNN")
    """
    from bt_filter import ARTIFACT_ROOT
    try:
        parts = video.resolve().relative_to(ARTIFACT_ROOT.resolve()).parts
    except ValueError:
        return None
    if len(parts) < 2 or parts[0] not in ("Movies", "TV"):
        return None
    if parts[0] == "Movies":
        return video.parent, parts[1]
    # TV: parts must be TV/<show>/Season NN/<file>
    if len(parts) < 4:
        return None
    show = parts[1]
    season_dir = parts[2]                                # e.g. "Season 01"
    season_short = season_dir.replace("Season ", "S")    # → "S01"
    return video.parent, f"{show} {season_short}"


def _group_video_siblings(group_dir: Path) -> list[Path]:
    """Every canonical video living directly inside `group_dir`. Used to
    compute the group's aggregation members."""
    try:
        return sorted(
            p for p in group_dir.iterdir()
            if p.is_file() and p.suffix.lower() in _VIDEO_EXTS
        )
    except OSError:
        return []


def _has_any_failure_sidecar(video: Path) -> Optional[tuple[str, str]]:
    """If the video has a failure sidecar (new .pipeline-failed or any
    legacy sidecar), return (kind, reason). Otherwise None. Delegates
    to srt_source for the sidecar recognition + parsing."""
    return any_failure_sidecar_with_kind(video)


def _group_terminal_state(group_dir: Path) -> Optional[dict]:
    """{total, succeeded, failed, all_terminal} for a pipeline group.
    Returns None if the directory has no video siblings.

    "Succeeded" = both canonical `.srt` and `.zh-tw.srt` exist (translation
    is the pipeline's final stage — English alone isn't consumable).
    "Failed" = any `.pipeline-failed` sidecar (whisper / annotate /
    translate stage, kind captured for the summary).
    """
    siblings = _group_video_siblings(group_dir)
    if not siblings:
        return None
    succeeded: list[Path] = []
    failed: list[tuple[Path, str, str]] = []
    for video in siblings:
        has_srt = video.with_suffix(".srt").exists()
        has_zh = (video.parent / f"{video.stem}.zh-tw.srt").exists()
        if has_srt and has_zh:
            succeeded.append(video)
            continue
        f = _has_any_failure_sidecar(video)
        if f is not None:
            kind, reason = f
            failed.append((video, kind, reason))
    return {
        "total": len(siblings),
        "succeeded": succeeded,
        "failed": failed,
        "all_terminal": len(succeeded) + len(failed) == len(siblings),
    }


# ── summary rendering ─────────────────────────────────────────────────

def _fmt_summary(label: str, state: dict) -> str:
    ok = len(state["succeeded"])
    bad = len(state["failed"])
    total = state["total"]
    verb = "done" if bad == 0 else "partial"
    line1 = f"Transcribe {verb} | {label} | {ok}/{total} ok"
    if bad:
        line1 += f" | {bad}/{total} failed"
    if bad == 0:
        return line1
    failed_bits = [f"{v.name} ({kind})" for v, kind, _ in state["failed"][:10]]
    tail = "Failed: " + ", ".join(failed_bits)
    if bad > 10:
        tail += f", …+{bad - 10}"
    return line1 + "\n" + tail


def _fmt_individual_success(video: Path, tier: str) -> str:
    return f"Transcribe done | {video.name} | tier={tier}"


def _fmt_individual_failure(video: Path, kind: str, reason: str) -> str:
    short = reason.replace("\n", " ")[:120]
    return f"Transcribe failed | {video.name} | {kind}: {short}"


# ── public API ─────────────────────────────────────────────────────────

def _maybe_fire_group(video: Path) -> bool:
    """Locate the video's group, check terminal state, fire if terminal.
    Returns True if the video belongs to a recognized group (caller
    skips individual notification either way — no wrapper spam)."""
    g = _find_group_for_video(video)
    if g is None:
        return False
    group_dir, label = g
    state = _group_terminal_state(group_dir)
    if state is None or not state["all_terminal"]:
        return True   # in-group but not terminal — still no individual send
    _send(_fmt_summary(label, state))
    # Wrapper fully done (English + Chinese both landed for every
    # video, modulo failures) → tell Jellyfin to re-index.
    from jellyfin_client import rescan_library
    rescan_library()
    return True


def notify_success(video: Path, tier: str) -> None:
    """Called by process_bt_file when a video completes ALL pipeline
    stages (whisper / verify / annotate / translate). Routes to a
    group-level summary if the video is in a recognized canonical
    layout, otherwise falls back to an individual notification (YT,
    manual drops)."""
    if not _maybe_fire_group(video):
        _send(_fmt_individual_success(video, tier))


def notify_failure(video: Path, kind: str, reason: str) -> None:
    """Called by process_bt_file when a stage failure sidecar is stamped.
    Same routing as `notify_success`."""
    if not _maybe_fire_group(video):
        _send(_fmt_individual_failure(video, kind, reason))


def notify_filter_failure(wrapper_name: str, reason: str) -> None:
    """Called by bt_filter.filter_wrapper when the wrapper's LLM analysis
    fails or produces something unusable (empty tree, missing regex,
    malformed schema, etc.) — cases where the wrapper never reaches
    per-video pipeline. Individual notification (no aggregation domain
    exists yet at filter time)."""
    short = reason.replace("\n", " ")[:150]
    _send(f"Transcribe filter failed | {wrapper_name} | {short}")
