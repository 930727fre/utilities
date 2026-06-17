"""OpenSubtitles.com client — find a matching subtitle for a video file.

Called before whisper for any qb video that's missing a strict-stem .srt. For
popular content (movies, mainstream TV) OpenSubtitles usually has a hand-
translated sub with proper punctuation and clean cue boundaries — much better
than whisper output, and free at the API layer (just 5-20 downloads/day on the
free tier per registered consumer).

Auth: `Api-Key` header for search; `Authorization: Bearer <token>` for download
endpoints. Token obtained via /login (user/pass), cached in memory, refreshed
on 401.

Silent-fail philosophy: any error path returns None and the caller falls
through to whisper. The pipeline degrades gracefully if env vars are unset,
quota is exhausted, or OpenSubtitles is unreachable.
"""
import os
import struct
import subprocess
import threading
from pathlib import Path
from typing import Optional

import requests

API_BASE = "https://api.opensubtitles.com/api/v1"
# Compose enforces these are set at parse time (${VAR:?error}), so we can fail
# loudly here too rather than silently no-op'ing later.
API_KEY = os.environ["OPENSUBTITLES_API_KEY"]
USERNAME = os.environ["OPENSUBTITLES_USERNAME"]
PASSWORD = os.environ["OPENSUBTITLES_PASSWORD"]
# Per OpenSubtitles ToS the User-Agent should identify the consumer registration.
USER_AGENT = os.environ.get("OPENSUBTITLES_USER_AGENT", "transcribe v1.0")

HASH_CHUNK = 65536  # 64 KB for OSDb hash

# Cached bearer token (set on first download call, refreshed on 401).
_token: Optional[str] = None
_token_lock = threading.Lock()

# Negative cache: paths where lookup found nothing. Reset on container restart.
# Without this we'd waste an API call every 30s loop iteration on the same miss.
_failed: set[str] = set()
_failed_lock = threading.Lock()


def _osdb_hash(path: Path) -> str:
    """OpenSubtitles file hash: 64-bit sum of (file size, first 64KB, last 64KB)
    interpreted as little-endian unsigned longs. Returns 16-char hex string.
    """
    size = path.stat().st_size
    if size < HASH_CHUNK * 2:
        raise ValueError(f"file too small for OSDb hash: {size} bytes")

    h = size
    fmt = "<Q"
    long_size = struct.calcsize(fmt)

    with open(path, "rb") as f:
        for _ in range(HASH_CHUNK // long_size):
            buf = f.read(long_size)
            if len(buf) < long_size:
                break
            h = (h + struct.unpack(fmt, buf)[0]) & 0xFFFFFFFFFFFFFFFF

        f.seek(max(0, size - HASH_CHUNK))
        for _ in range(HASH_CHUNK // long_size):
            buf = f.read(long_size)
            if len(buf) < long_size:
                break
            h = (h + struct.unpack(fmt, buf)[0]) & 0xFFFFFFFFFFFFFFFF

    return f"{h:016x}"


def _headers(with_token: bool = False) -> dict:
    h = {
        "Api-Key": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if with_token and _token:
        h["Authorization"] = f"Bearer {_token}"
    return h


def _login():
    """POST /login to obtain bearer token. Caches globally."""
    global _token
    r = requests.post(
        f"{API_BASE}/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    with _token_lock:
        _token = r.json()["token"]


def _download_with_retry(file_id: int) -> str:
    """POST /download → returns the actual download URL. Re-logins once on 401.

    Lock discipline: only hold _token_lock for short read/writes of _token;
    never call _login() while holding the lock (it acquires _token_lock too,
    and threading.Lock is not reentrant — would deadlock).
    """
    global _token
    with _token_lock:
        need_login = _token is None
    if need_login:
        _login()

    r = requests.post(
        f"{API_BASE}/download",
        json={"file_id": file_id},
        headers=_headers(with_token=True),
        timeout=15,
    )
    if r.status_code == 401:
        with _token_lock:
            _token = None
        _login()
        r = requests.post(
            f"{API_BASE}/download",
            json={"file_id": file_id},
            headers=_headers(with_token=True),
            timeout=15,
        )
    r.raise_for_status()
    return r.json()["link"]


def find_subs(video: Path) -> Optional[Path]:
    """Search OpenSubtitles for an English subtitle matching this video.

    Strategy: hash the file, search by moviehash for highest-confidence match.
    If a result exists, download and write `<video stem>.srt`. Returns the
    written path on success, None on any miss/error.
    """
    with _failed_lock:
        if str(video) in _failed:
            return None

    def cache_miss():
        with _failed_lock:
            _failed.add(str(video))
        return None

    try:
        h = _osdb_hash(video)
    except (OSError, ValueError) as e:
        print(f"[subs-finder] hash failed for {video.name!r}: {e}", flush=True)
        return cache_miss()

    try:
        r = requests.get(
            f"{API_BASE}/subtitles",
            params={"moviehash": h, "languages": "en"},
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
    except requests.RequestException as e:
        print(f"[subs-finder] search failed for {video.name!r}: {e}", flush=True)
        return cache_miss()

    if not data:
        return cache_miss()

    # Prefer results where the API confirmed an exact hash match — anything
    # else can be sub for a different release of the same movie (slightly
    # different intro logo / cut → sync drift of seconds). Fall back to the
    # first result only if no strict matches exist.
    hash_exact = [d for d in data if (d.get("attributes") or {}).get("moviehash_match")]
    pick = hash_exact[0] if hash_exact else data[0]

    attrs = pick.get("attributes") or {}
    files = attrs.get("files") or []
    if not files:
        return cache_miss()
    file_id = files[0].get("file_id")
    if not file_id:
        return cache_miss()
    print(f"[subs-finder] picked release={attrs.get('release', '?')!r} "
          f"hash_exact={bool(hash_exact)}", flush=True)

    try:
        download_url = _download_with_retry(file_id)
    except (requests.RequestException, KeyError) as e:
        print(f"[subs-finder] download API failed for {video.name!r}: {e}", flush=True)
        return cache_miss()

    try:
        r = requests.get(download_url, timeout=60)
        r.raise_for_status()
        # OS subs come in varied encodings; let requests decode with charset
        # detection, then we write as UTF-8 (annotation step also handles this
        # with a latin-1 fallback if needed).
        srt_text = r.text
    except requests.RequestException as e:
        print(f"[subs-finder] SRT fetch failed for {video.name!r}: {e}", flush=True)
        return cache_miss()

    if not srt_text.strip():
        return cache_miss()

    out = video.with_suffix(".srt")
    out.write_text(srt_text, encoding="utf-8")
    print(f"[subs-finder] wrote {out.name!r} from OpenSubtitles", flush=True)

    _resync_inplace(video, out)
    return out


def _resync_inplace(video: Path, srt: Path) -> None:
    """ffsubsync uses VAD on the video's audio + the SRT's cue rhythm to find
    the right time offset, then writes the corrected SRT back in place.

    Best-effort: any failure leaves the original (possibly mis-synced) SRT
    intact and the pipeline continues. Capped at 5 min for a sane upper bound
    on long movies.
    """
    try:
        r = subprocess.run(
            ["ffsubsync", str(video), "-i", str(srt), "-o", str(srt)],
            capture_output=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[subs-finder] ffsubsync error for {srt.name!r}: {e}", flush=True)
        return
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-1:]
        print(f"[subs-finder] ffsubsync rc={r.returncode} for {srt.name!r}: {tail}", flush=True)
        return
    print(f"[subs-finder] resynced {srt.name!r} via ffsubsync", flush=True)
