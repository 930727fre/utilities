"""Trigger Jellyfin library rescan after pipeline events.

Called from `notifier._maybe_fire_group` at the same moment a wrapper
summary fires — season-terminal state means new canonical / .zh-tw.srt
files have landed and Jellyfin should re-index the library.

Uses the "refresh whole library" endpoint (`POST /Library/Refresh`) —
simpler than per-folder targeting: no path translation between the
transcribe container's mount view (`/app/data/artifact/…`) and the
Jellyfin container's mount view (`/media/artifact/…`), no library ID
lookup. Jellyfin's incremental scan is cheap when nothing changed for
most items, so a full-library refresh a few dozen times per day is
negligible cost.

Config (both required at compose parse time):
  JELLYFIN_URL      — e.g. http://jellyfin:8096 (container name on my_network)
  JELLYFIN_API_KEY  — from Jellyfin dashboard → API Keys → new
"""
import os
import httpx

_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
_API_TIMEOUT = 5.0


def _enabled() -> bool:
    return bool(_URL and _API_KEY)


def rescan_library() -> None:
    """Fire-and-forget POST to trigger a full Jellyfin library scan.
    Silent no-op if not configured. Errors logged, never raised —
    Jellyfin availability shouldn't block the transcribe pipeline."""
    if not _enabled():
        return
    try:
        r = httpx.post(
            f"{_URL}/Library/Refresh",
            headers={"X-Emby-Token": _API_KEY},
            timeout=_API_TIMEOUT,
        )
        if r.status_code >= 400:
            print(f"[jellyfin] rescan returned {r.status_code}: {r.text[:200]}", flush=True)
    except httpx.HTTPError as exc:
        print(f"[jellyfin] rescan failed: {exc}", flush=True)
