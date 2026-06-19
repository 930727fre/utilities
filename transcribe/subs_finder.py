"""OpenSubtitles.com client — find a matching subtitle for a video file.

Called before whisper for any qb video that's missing a strict-stem .srt. For
popular content (movies, mainstream TV) OpenSubtitles usually has a hand-
translated sub with proper punctuation and clean cue boundaries — much better
than whisper output, and free at the API layer (just 5-20 downloads/day on the
free tier per registered consumer).

Two-pass search:
1. **hash search** — query by OSDb file hash, filter to `moviehash_match=True`,
   verify via Haiku. Highest confidence — when it hits, the SRT is for this
   exact release, no timing drift.
2. **text search** — fall back to querying by title (+ year for movies,
   + season/episode for TV episodes), verify via Haiku, run ffsubsync to fix
   release-mismatch drift. Lower confidence — the SRT is for a different
   release, so timing alignment relies on ffsubsync.

Auth: `Api-Key` header for search; `Authorization: Bearer <token>` for download
endpoints. Token obtained via /login (user/pass), cached in memory, refreshed
on 401.

Silent-fail philosophy: any error path returns None and the caller falls
through to whisper. The pipeline degrades gracefully if env vars are unset,
quota is exhausted, or OpenSubtitles is unreachable.
"""
import os
import re
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from srt_source import stamp_source
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

# Negative cache: { video_path: expiry_unix_timestamp }.
#
# Without it the 30 s scan loop would burn an OS API call (and a Claude haiku
# verify, which costs real money) every tick on the same video.
#
# Permanent entries (`float("inf")`) are for outcomes that the next API call
# would deterministically reproduce — no candidate found, all candidates
# rejected by the verifier, candidate has no file_id. Time-limited entries
# are for "transient-looking" failures, almost always OpenSubtitles' daily
# download quota cap (HTTP 406): the quota refreshes at the next UTC day
# boundary, so we expire those after 24 h and let whichever scan tick lands
# first try the API again.
_failed: dict[str, float] = {}
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


# ── Two search strategies ─────────────────────────────────────────────────

def _search_by_hash(video: Path) -> Optional[dict]:
    """Hash-based search. Returns the verified candidate or None."""
    try:
        h = _osdb_hash(video)
    except (OSError, ValueError) as e:
        print(f"[subs-finder] hash failed for {video.name!r}: {e}", flush=True)
        return None

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
        print(f"[subs-finder] hash search failed for {video.name!r}: {e}", flush=True)
        return None

    if not data:
        print(f"[subs-finder] hash 0 results for {video.name!r}", flush=True)
        return None

    # `moviehash_match` is uploader-claimed (not server-verified); Haiku
    # downstream catches the mis-tags that this filter alone misses.
    hash_exact = [d for d in data if (d.get("attributes") or {}).get("moviehash_match")]
    if not hash_exact:
        print(f"[subs-finder] hash {len(data)} results, 0 hash-exact for "
              f"{video.name!r}", flush=True)
        return None

    return verify_candidate(video, hash_exact)


def _search_by_text(video: Path) -> Optional[dict]:
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

    params: dict = {"query": info["title"], "languages": "en"}
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

    # Pass up to top 10 to the verifier — text search returns ordered by
    # relevance, more than that just burns tokens without helping.
    print(f"[subs-finder] text {len(data)} results for {video.name!r}", flush=True)
    return verify_candidate(video, data[:10])


# ── Entry point ───────────────────────────────────────────────────────────

def find_subs(video: Path) -> Optional[Path]:
    """Search OpenSubtitles for an English subtitle matching this video.

    Try hash search first (zero timing drift when it hits), fall back to
    text search (drift fixed by ffsubsync). Returns the written SRT path
    on success, None on any miss/error.
    """
    now = time.time()
    with _failed_lock:
        expiry = _failed.get(str(video))
        if expiry is not None:
            if expiry > now:
                return None
            # Expired — drop the entry so we don't keep checking expiry.
            _failed.pop(str(video), None)

    def cache_permanent():
        with _failed_lock:
            _failed[str(video)] = float("inf")
        return None

    def cache_transient():
        with _failed_lock:
            _failed[str(video)] = time.time() + _TRANSIENT_RETRY_SECONDS
        return None

    pick = _search_by_hash(video)
    source_tag = "opensubtitles-hash"

    if pick is None:
        pick = _search_by_text(video)
        source_tag = "opensubtitles-text"

    if pick is None:
        return cache_permanent()

    attrs = pick.get("attributes") or {}
    files = attrs.get("files") or []
    if not files:
        print(f"[subs-finder] picked candidate has no files for {video.name!r}", flush=True)
        return cache_permanent()
    file_id = files[0].get("file_id")
    if not file_id:
        print(f"[subs-finder] picked candidate has no file_id for {video.name!r}", flush=True)
        return cache_permanent()
    print(f"[subs-finder] picked release={attrs.get('release', '?')!r} "
          f"via {source_tag}", flush=True)

    try:
        download_url = _download_with_retry(file_id)
    except (requests.RequestException, KeyError) as e:
        # Almost always HTTP 406 = daily quota exhausted, sometimes a real
        # network blip. Either way, "tomorrow / soon" is the right retry
        # window, not "permanent" or "every 30 s."
        print(f"[subs-finder] download API failed for {video.name!r}: {e}", flush=True)
        return cache_transient()

    try:
        r = requests.get(download_url, timeout=60)
        r.raise_for_status()
        # OS subs come in varied encodings; let requests decode with charset
        # detection, then we write as UTF-8 (annotation step also handles this
        # with a latin-1 fallback if needed).
        srt_text = r.text
    except requests.RequestException as e:
        # CDN hiccup — also transient.
        print(f"[subs-finder] SRT fetch failed for {video.name!r}: {e}", flush=True)
        return cache_transient()

    if not srt_text.strip():
        # Genuinely missing content on OpenSubtitles' side — no point
        # retrying tomorrow.
        print(f"[subs-finder] empty SRT body for {video.name!r}", flush=True)
        return cache_permanent()

    out = video.with_suffix(".srt")
    out.write_text(srt_text, encoding="utf-8")
    print(f"[subs-finder] wrote {out.name!r} from OpenSubtitles", flush=True)

    # ffsubsync overwrites the SRT, so source-stamp AFTER it (whether it
    # succeeded or fell through). Tag distinguishes hash vs text origin —
    # text-origin SRTs are more likely to need timing verification.
    _resync_inplace(video, out)
    stamp_source(out, source_tag)
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
