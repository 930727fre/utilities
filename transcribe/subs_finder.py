"""OpenSubtitles.com client — fetch matching subtitle candidates for a video.

Called by the bt video pipeline AFTER whisper has produced a ground-truth
SRT. The candidates returned here are evaluated against whisper output by
`subs_verifier.verify_against_whisper`; only those that pass that content
gate become the final subtitle. Mis-matched candidates are simply
discarded — there's no second-chance lookup or stamping back into the
canonical SRT path.

This module's sole responsibility:
  1. Search OpenSubtitles (hash, then text)
  2. Metadata-prefilter via Haiku (`subs_verifier.verify_candidate`)
  3. Download the chosen candidate's raw SRT bytes into a caller-provided
     destination path

Caller does ffsubsync, caller decides whether to promote the candidate
to canonical. This module never touches `_sources/` layout or
`/artifact/.../*.srt` directly — purity matters because the candidate
might lose verification and we don't want partial writes leaking into
user-facing paths.

Auth: `Api-Key` header for search; `Authorization: Bearer <token>` for
download endpoints. Token obtained via /login, cached in memory,
refreshed on 401.

Quota: 5-20 downloads/day on the free tier (300 with paid). A daily-
TTL negative cache keyed by (video, languages, mode) prevents the
30 s scan loop from re-burning quota on a video that just missed.
"""
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from subs_verifier import verify_candidate

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

# Negative cache: { (video_path, languages, mode): expiry_unix_timestamp }
# `mode` ∈ {"hash", "text"} — both legs cached independently so a quota
# hit on /download (which only fires after a successful search) doesn't
# poison the cheaper search path on the other mode.
#
# Permanent entries (`expiry = float("inf")`) are for outcomes that the
# next API call would deterministically reproduce — no results, all
# candidates rejected by Haiku, candidate had no file_id. Time-limited
# entries (24 h TTL) cover quota exhaustion (HTTP 406) so the next-day
# scan tick gets a clean retry without our 30 s loop spamming OS.
_failed: dict[tuple[str, str, str], float] = {}
_failed_lock = threading.Lock()

_TRANSIENT_RETRY_SECONDS = 24 * 3600


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


# ── Filename parsing for text search ──────────────────────────────────────

_TV_PATTERN = re.compile(r"\bS(\d{1,2})E(\d{1,3})\b", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")


def _parse_filename(video: Path) -> dict:
    """Best-effort title / year / season / episode extraction.

    Used to build OS text-search queries. Heuristic only; the Haiku verifier
    filters bad matches downstream, so over-matching here is fine.
    """
    cleaned = re.sub(r"[._]", " ", video.stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    season: Optional[int] = None
    episode: Optional[int] = None
    title_part = cleaned

    m_tv = _TV_PATTERN.search(cleaned)
    if m_tv:
        season = int(m_tv.group(1))
        episode = int(m_tv.group(2))
        title_part = cleaned[:m_tv.start()].rstrip(" -")
    else:
        m_year = _YEAR_PATTERN.search(cleaned)
        if m_year:
            title_part = cleaned[:m_year.start()].rstrip(" -")

    # Drop parenthetical (YYYY) inside title.
    title_part = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", "", title_part)
    title_part = re.sub(r"\s+", " ", title_part).strip(" -")

    year_match = _YEAR_PATTERN.search(cleaned)
    year = int(year_match.group()) if year_match else None

    return {
        "title": title_part,
        "year": year,
        "season": season,
        "episode": episode,
    }


# ── Failed cache helpers ──────────────────────────────────────────────────

def _check_cache(key: tuple[str, str, str]) -> bool:
    """Return True if this lookup should be skipped (cached as failed)."""
    now = time.time()
    with _failed_lock:
        expiry = _failed.get(key)
        if expiry is None:
            return False
        if expiry > now:
            return True
        # Expired — drop so we don't keep checking expiry.
        _failed.pop(key, None)
        return False


def _cache_permanent(key: tuple[str, str, str]):
    with _failed_lock:
        _failed[key] = float("inf")


def _cache_transient(key: tuple[str, str, str]):
    with _failed_lock:
        _failed[key] = time.time() + _TRANSIENT_RETRY_SECONDS


# ── Two search strategies ─────────────────────────────────────────────────

def _search_by_hash(video: Path, languages: str) -> Optional[dict]:
    """Hash-based search. Returns the verified candidate or None."""
    try:
        h = _osdb_hash(video)
    except (OSError, ValueError) as e:
        print(f"[subs-finder] hash failed for {video.name!r}: {e}", flush=True)
        return None

    try:
        r = requests.get(
            f"{API_BASE}/subtitles",
            params={"moviehash": h, "languages": languages},
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
    except requests.RequestException as e:
        print(f"[subs-finder] hash search failed for {video.name!r}: {e}", flush=True)
        return None

    if not data:
        print(f"[subs-finder] hash 0 results for {video.name!r} (lang={languages})", flush=True)
        return None

    # `moviehash_match` is uploader-claimed (not server-verified); Haiku
    # downstream catches the mis-tags that this filter alone misses.
    hash_exact = [d for d in data if (d.get("attributes") or {}).get("moviehash_match")]
    if not hash_exact:
        print(f"[subs-finder] hash {len(data)} results, 0 hash-exact for "
              f"{video.name!r}", flush=True)
        return None

    return verify_candidate(video, hash_exact)


def _search_by_text(video: Path, languages: str) -> Optional[dict]:
    """Text-based search using title + (year | season/episode). Returns the
    verified candidate or None.

    Useful when hash hits empty (sparse coverage for niche releases / TV).
    The candidate's release won't match the local file, so ffsubsync is
    needed downstream to fix timing drift.
    """
    info = _parse_filename(video)
    if not info["title"]:
        print(f"[subs-finder] text: could not extract title from {video.name!r}", flush=True)
        return None

    params: dict = {"query": info["title"], "languages": languages}
    if info["season"] is not None and info["episode"] is not None:
        params["season_number"] = info["season"]
        params["episode_number"] = info["episode"]
    elif info["year"]:
        # Year only helps narrow down for movies — for TV it's first-air year
        # of the show, which may differ from episode air year and hurt recall.
        params["year"] = info["year"]

    try:
        r = requests.get(
            f"{API_BASE}/subtitles",
            params=params,
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
    except requests.RequestException as e:
        print(f"[subs-finder] text search failed for {video.name!r}: {e}", flush=True)
        return None

    if not data:
        print(f"[subs-finder] text 0 results for {video.name!r} "
              f"(query={info['title']!r} S={info['season']} E={info['episode']} y={info['year']})", flush=True)
        return None

    print(f"[subs-finder] text {len(data)} results for {video.name!r}", flush=True)
    return verify_candidate(video, data[:10])


# ── Download a verified pick to a destination path ────────────────────────

def _download_candidate(pick: dict, dest: Path) -> bool:
    """Resolve the chosen OS pick into actual SRT bytes at `dest`.
    Returns True on success, False on any failure (caller decides
    whether to cache permanent vs transient based on its key)."""
    attrs = pick.get("attributes") or {}
    files = attrs.get("files") or []
    if not files:
        return False
    file_id = files[0].get("file_id")
    if not file_id:
        return False

    try:
        download_url = _download_with_retry(file_id)
    except (requests.RequestException, KeyError) as e:
        # Almost always HTTP 406 (daily quota); caller treats as transient.
        print(f"[subs-finder] /download API failed: {e}", flush=True)
        return False

    try:
        r = requests.get(download_url, timeout=60)
        r.raise_for_status()
        srt_text = r.text
    except requests.RequestException as e:
        print(f"[subs-finder] SRT fetch failed: {e}", flush=True)
        return False

    if not srt_text.strip():
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(srt_text, encoding="utf-8")
    return True


# ── Public entry points: per-mode candidate fetch ─────────────────────────

def find_candidate_hash(video: Path, languages: str, dest: Path) -> Optional[Path]:
    """Fetch one hash-matched candidate SRT to `dest`. Returns `dest` on
    success, None on any miss.

    Caller is responsible for whisper-verifying the result and deciding
    whether to promote to canonical."""
    key = (str(video), languages, "hash")
    if _check_cache(key):
        return None

    pick = _search_by_hash(video, languages)
    if pick is None:
        _cache_permanent(key)
        return None

    if not _download_candidate(pick, dest):
        # /download endpoint is the quota-gated one; treat as transient.
        _cache_transient(key)
        return None

    attrs = pick.get("attributes") or {}
    print(f"[subs-finder] hash candidate written: release="
          f"{attrs.get('release', '?')!r} → {dest.name}", flush=True)
    return dest


def find_candidate_text(video: Path, languages: str, dest: Path) -> Optional[Path]:
    """Fetch one text-matched candidate SRT to `dest`. Returns `dest` on
    success, None on any miss.

    Caller is responsible for whisper-verifying the result and running
    ffsubsync against the local audio."""
    key = (str(video), languages, "text")
    if _check_cache(key):
        return None

    pick = _search_by_text(video, languages)
    if pick is None:
        _cache_permanent(key)
        return None

    if not _download_candidate(pick, dest):
        _cache_transient(key)
        return None

    attrs = pick.get("attributes") or {}
    print(f"[subs-finder] text candidate written: release="
          f"{attrs.get('release', '?')!r} → {dest.name}", flush=True)
    return dest
