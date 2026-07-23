"""aria2c daemon + JSON-RPC client.

Design contract:
  * ONE persistent aria2c daemon in this container, RPC on 127.0.0.1:6800.
    All torrents share that daemon — global `--max-overall-upload-limit`
    is a real hard cap across every seed, not the per-process soft cap
    the old spawn-per-magnet layout gave us.
  * Sidecar (FastAPI) is a thin translator: HTTP → aria2 RPC. State
    lives in the daemon + on disk under `/data/bt`, never in this
    module beyond in-flight caches.
  * Each torrent still lands in its own per-torrent wrapper folder
    under `/data/bt` — we derive the folder from the magnet's `dn=`
    up front and pass it to aria2 as the `dir` option. That keeps
    the transcribe pipeline (which watches wrappers, not raw file
    paths) unchanged.
  * Startup recovery: sidecar scans `/data/bt/**/*.torrent` and
    re-adds any torrent the daemon doesn't already know about. aria2
    finds the existing `.aria2` control file, skips already-verified
    pieces, resumes downloading / seeding seamlessly.

RPC secret comes from ARIA2_RPC_TOKEN env — required, no fallback
(exposed RPC without auth is a remote-code-exec vector).
"""
import base64
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

BT_LIBRARY = Path("/data/bt")

# Session file — aria2 persists its active+waiting queue here every
# --save-session-interval seconds AND on graceful shutdown, then loads
# it via --input-file on the next start. Lives inside /data/bt so it
# survives container recreate.
SESSION_FILE = BT_LIBRARY / ".aria2-session"

# Seed limits.
#
# SEED_TIME_MIN: minutes to seed after download completes. Set to 0
# meaning "no time limit" in OUR code — aria2 itself treats
# --seed-time=0 as "don't seed at all" (opposite of what we want), so
# _daemon_cmd() omits the flag entirely when this is 0.
#
# SEED_RATIO: seed until this share ratio is reached. 0.0 in aria2's
# native language means "no ratio limit — seed forever" (yes, 0 means
# unlimited here, not zero).
#
# Both at "no limit" = seed indefinitely subject to the global upload
# cap. Community-friendly, protected against runaway by
# GLOBAL_UPLOAD_LIMIT.
SEED_TIME_MIN = 0
SEED_RATIO = 0.0

# GLOBAL upload cap across ALL torrents this daemon manages. 25M = 25
# MB/s ≈ 200 Mbps, sized against the user's 300 Mbps uplink policy of
# "leave ~100 Mbps headroom for jellyfin / SSH / backup / anything
# else on the host". Unlike the old per-process cap × concurrent
# torrents (which spiked past aggregate target when many popular
# torrents seeded at once), this is a real hard ceiling regardless
# of how many torrents are active.
GLOBAL_UPLOAD_LIMIT = "25M"

_RPC_URL = "http://127.0.0.1:6800/jsonrpc"
_RPC_TOKEN = os.environ.get("ARIA2_RPC_TOKEN", "").strip()
_RPC_TIMEOUT = 10.0

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Daemon subprocess handle — set by start_daemon(), read by shutdown().
_daemon: subprocess.Popen | None = None


# ── Daemon lifecycle ──────────────────────────────────────────────────────

def _daemon_cmd() -> list[str]:
    if not _RPC_TOKEN:
        raise RuntimeError(
            "ARIA2_RPC_TOKEN env var is empty — required for daemon auth "
            "(unauthenticated RPC on 6800 is a remote code execution risk)"
        )
    BT_LIBRARY.mkdir(parents=True, exist_ok=True)
    session_arg: list[str] = [f"--save-session={SESSION_FILE}",
                              "--save-session-interval=60"]
    # Only pass --input-file if the session actually exists. aria2c
    # aborts with "unrecognized URI or option" if we point it at a
    # non-existent input-file on first boot.
    if SESSION_FILE.exists():
        session_arg.append(f"--input-file={SESSION_FILE}")
    cmd = [
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        "--rpc-listen-port=6800",
        f"--rpc-secret={_RPC_TOKEN}",
        f"--dir={BT_LIBRARY}",
        f"--seed-ratio={SEED_RATIO}",
        f"--max-overall-upload-limit={GLOBAL_UPLOAD_LIMIT}",
        "--bt-save-metadata=true",
        # Save completed / seeding torrents to session too — default
        # --save-session only stores unfinished downloads, so a
        # rebuild would silently drop every completed torrent from
        # the seed queue. With --force-save=true they survive across
        # daemon restarts (and rebuilds).
        "--force-save=true",
        "--continue=true",
        "--auto-file-renaming=false",
        "--enable-color=false",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--daemon=false",
        *session_arg,
    ]
    # Skip --seed-time entirely when SEED_TIME_MIN == 0: aria2 reads
    # --seed-time=0 as "disable seeding" (opposite of our meaning).
    # Omitting the flag lets --seed-ratio be the only cap.
    if SEED_TIME_MIN > 0:
        cmd.append(f"--seed-time={SEED_TIME_MIN}")
    return cmd


def _wait_for_rpc(deadline_sec: float = 15.0) -> None:
    deadline = time.time() + deadline_sec
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            # getVersion is the cheapest call that also validates auth.
            _rpc("aria2.getVersion")
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(0.2)
    raise RuntimeError(f"aria2c RPC not ready after {deadline_sec}s: {last_exc}")


def start_daemon() -> None:
    """Launch aria2c as a foreground subprocess, then block until its
    RPC endpoint accepts requests. Idempotent — noop if already up."""
    global _daemon
    if _daemon is not None and _daemon.poll() is None:
        return
    cmd = _daemon_cmd()
    seed_time_desc = f"{SEED_TIME_MIN}m" if SEED_TIME_MIN > 0 else "∞"
    ratio_desc = f"{SEED_RATIO}" if SEED_RATIO > 0 else "∞"
    print(f"[aria2c] starting daemon: cap={GLOBAL_UPLOAD_LIMIT} "
          f"seed_time={seed_time_desc} ratio={ratio_desc}", flush=True)
    _daemon = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )

    def _drain_stderr() -> None:
        assert _daemon is not None and _daemon.stderr is not None
        for raw in _daemon.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[aria2c] {text}", flush=True)

    threading.Thread(target=_drain_stderr, daemon=True).start()
    _wait_for_rpc()
    print("[aria2c] RPC ready", flush=True)


def stop_daemon() -> None:
    """Ask aria2c to shut down gracefully (flushes session, closes
    peer connections). Falls back to SIGTERM/SIGKILL if RPC is dead."""
    global _daemon
    if _daemon is None:
        return
    try:
        # aria2.shutdown returns before shutdown completes; the process
        # exits shortly after. Bounded wait below prevents indefinite
        # hang if aria2c is stuck.
        _rpc("aria2.shutdown")
    except Exception as exc:
        print(f"[aria2c] graceful shutdown RPC failed ({exc}); "
              "falling back to SIGTERM", flush=True)
        _daemon.terminate()
    try:
        _daemon.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _daemon.kill()
        _daemon.wait(timeout=5)
    _daemon = None
    print("[aria2c] daemon exited", flush=True)


# ── RPC client ────────────────────────────────────────────────────────────

def _rpc(method: str, *params: Any) -> Any:
    """One JSON-RPC call. Prepends the secret token to params (aria2
    convention: first param is `"token:<secret>"`). Raises RuntimeError
    on transport / protocol error, or if aria2 returns an error object."""
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": method,
        "params": [f"token:{_RPC_TOKEN}", *params],
    }
    r = requests.post(_RPC_URL, json=body, timeout=_RPC_TIMEOUT)
    r.raise_for_status()
    resp = r.json()
    if "error" in resp:
        err = resp["error"]
        raise RuntimeError(f"aria2 RPC error [{err.get('code')}]: {err.get('message')}")
    return resp.get("result")


# ── Wrapper folder naming (unchanged from spawn-per-magnet era) ───────────

def _magnet_display_name(magnet: str) -> str | None:
    try:
        u = urlparse(magnet)
        dn = parse_qs(u.query).get("dn", [None])[0]
        if dn:
            return unquote(dn)
    except Exception:
        pass
    return None


def _infohash_from_magnet(magnet: str) -> str | None:
    """Extract 40-char lowercase hex infohash from a magnet URI's
    xt=urn:btih:XXX param. Accepts both hex (40 chars) and base32
    (32 chars) encodings. Returns None on any parse failure."""
    try:
        u = urlparse(magnet)
    except Exception:
        return None
    if u.scheme != "magnet":
        return None
    for xt in parse_qs(u.query).get("xt", []):
        if not xt.startswith("urn:btih:"):
            continue
        raw = xt[len("urn:btih:"):]
        if len(raw) == 40:
            try:
                bytes.fromhex(raw)
                return raw.lower()
            except ValueError:
                continue
        if len(raw) == 32:
            try:
                return base64.b32decode(raw.upper()).hex().lower()
            except Exception:
                continue
    return None


class DuplicateMagnetError(RuntimeError):
    """Magnet's infohash is already known to aria2. Callers can grab
    `.wrapper` for the existing download's wrapper folder name."""
    def __init__(self, wrapper: str):
        super().__init__(f"infohash already in aria2 (wrapper={wrapper!r})")
        self.wrapper = wrapper


def _safe_folder(name: str) -> str:
    safe = _UNSAFE_NAME.sub("_", name).strip(" .\t\n")
    return safe[:180] or "torrent"


def _pick_wrapper_dir(magnet: str) -> Path:
    """Reserve a unique per-torrent wrapper dir under /data/bt. Suffix
    with ` (2)`, ` (3)`, ... if the display name collides."""
    base = _safe_folder(_magnet_display_name(magnet) or "torrent")
    candidate = BT_LIBRARY / base
    i = 2
    while candidate.exists():
        candidate = BT_LIBRARY / f"{base} ({i})"
        i += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


# ── Torrent listing / discovery from aria2 state ──────────────────────────

def _all_downloads() -> list[dict]:
    """Every download aria2 currently knows about (active + waiting +
    stopped). tellStopped needs a page; 1000 is far more than we'd
    ever accumulate."""
    active = _rpc("aria2.tellActive") or []
    waiting = _rpc("aria2.tellWaiting", 0, 1000) or []
    stopped = _rpc("aria2.tellStopped", 0, 1000) or []
    return [*active, *waiting, *stopped]


def _known_infohashes() -> set[str]:
    """Infohashes aria2 currently knows about. Used by resume_all to
    avoid double-adding a .torrent that's already in the queue."""
    out: set[str] = set()
    for d in _all_downloads():
        ih = (d.get("infoHash") or "").lower()
        if ih:
            out.add(ih)
    return out


def _wrapper_from_download(d: dict) -> str | None:
    """Extract wrapper folder name from a download's `dir` field.
    Returns None if the dir isn't under BT_LIBRARY (shouldn't happen
    with our layout, but defensive)."""
    dir_str = d.get("dir")
    if not dir_str:
        return None
    try:
        rel = Path(dir_str).resolve().relative_to(BT_LIBRARY.resolve())
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if parts else None


def _find_gids_for_wrapper(wrapper_name: str) -> list[str]:
    """Every GID whose `dir` sits inside the wrapper folder. Usually
    one, but a defensive list — deletes touch all of them."""
    return [
        d["gid"]
        for d in _all_downloads()
        if _wrapper_from_download(d) == wrapper_name and d.get("gid")
    ]


def global_stats() -> dict:
    """Live bandwidth + cumulative transfer for the BT-tab header.

    download_speed / upload_speed come from aria2.getGlobalStat (bytes
    per second, current). total_downloaded / total_uploaded / ratio are
    summed across every torrent aria2 knows about (active + waiting +
    stopped) — cumulative since each torrent was first added and
    surviving daemon restarts via --force-save. Ratio semantics match
    what qBittorrent labels "all-time ratio": total_uploaded /
    total_downloaded, treating the whole library as one big torrent."""
    gs = _rpc("aria2.getGlobalStat") or {}
    downloads = _all_downloads()
    total_down = sum(int(d.get("completedLength") or 0) for d in downloads)
    total_up = sum(int(d.get("uploadLength") or 0) for d in downloads)
    ratio = (total_up / total_down) if total_down > 0 else 0.0
    return {
        "download_speed": int(gs.get("downloadSpeed") or 0),
        "upload_speed": int(gs.get("uploadSpeed") or 0),
        "total_downloaded": total_down,
        "total_uploaded": total_up,
        "ratio": round(ratio, 3),
        "active_count": int(gs.get("numActive") or 0),
    }


# ── Public API used by main.py ────────────────────────────────────────────

_submit_lock = threading.Lock()


def submit(magnet: str) -> str:
    """Reserve a wrapper dir and hand the magnet to aria2 with that
    dir pinned as the download destination. Returns wrapper name.

    Raises DuplicateMagnetError if aria2 already knows this magnet's
    infohash (active, seeding, complete, errored — anything in
    tellActive/Waiting/Stopped). Prevents accidental double-adds that
    would result in two GIDs for the same infohash: as we saw with
    Fringe on 2026-07-23, the second addUri creates a live GID while
    the first stays in stopped/complete, and _all_downloads's naive
    last-wins indexing makes the UI report the wrong state.

    The check-then-addUri is serialized with a module-level lock so
    two concurrent submits of the same magnet can't both pass the dedup
    check (which would happen without the lock — FastAPI runs sync
    endpoints in a threadpool, so two /torrents requests really do race).
    Cost is negligible for a single-user deployment: addUri is a few
    hundred ms and submits are rare."""
    with _submit_lock:
        info_hash = _infohash_from_magnet(magnet)
        if info_hash:
            for d in _all_downloads():
                if (d.get("infoHash") or "").lower() == info_hash:
                    raise DuplicateMagnetError(_wrapper_from_download(d) or "(unknown)")
        wrapper = _pick_wrapper_dir(magnet)
        print(f"[aria2c] adding magnet → {wrapper.name}", flush=True)
        _rpc("aria2.addUri", [magnet], {"dir": str(wrapper)})
        return wrapper.name


def list_torrents() -> list[dict]:
    """One entry per wrapper folder under /data/bt, phase + progress
    derived from aria2's view."""
    if not BT_LIBRARY.exists():
        return []

    # Fetch once, index by wrapper name.
    by_wrapper: dict[str, dict] = {}
    for d in _all_downloads():
        name = _wrapper_from_download(d)
        if name:
            by_wrapper[name] = d

    out: list[dict] = []
    for wrapper in sorted(BT_LIBRARY.iterdir()):
        if not wrapper.is_dir():
            continue
        # Skip our own session file — it's a file, not a dir, but iterdir
        # returns both; the is_dir() above already filters. Belt-and-
        # suspenders: also skip hidden entries.
        if wrapper.name.startswith("."):
            continue
        d = by_wrapper.get(wrapper.name)
        phase = _phase(wrapper, d)
        row = {"name": wrapper.name, "phase": phase}
        if phase == "downloading" and d:
            try:
                completed = int(d.get("completedLength") or 0)
                total = int(d.get("totalLength") or 0)
                if total > 0:
                    row["progress"] = {"completed": completed, "total": total}
            except (ValueError, TypeError):
                pass
        out.append(row)
    return out


def _phase(wrapper: Path, d: dict | None) -> str:
    """downloading / seeding / done / orphaned."""
    if d is None:
        # aria2 doesn't know about this wrapper — user manually dropped
        # files, or startup scan hasn't reached it yet.
        return "orphaned"
    status = d.get("status")  # active / waiting / paused / error / complete / removed
    if status == "active":
        # active means aria2 is working on it — download or seed. Use
        # completedLength vs totalLength to distinguish.
        completed = int(d.get("completedLength") or 0)
        total = int(d.get("totalLength") or 0)
        if total > 0 and completed >= total:
            return "seeding"
        return "downloading"
    if status in ("complete", "removed"):
        return "done"
    if status == "paused":
        return "downloading"
    return "orphaned"


def delete(wrapper_name: str) -> None:
    """Remove all aria2 downloads targeting this wrapper.

    Two-step removal: forceRemove ends the download+seed session,
    removeDownloadResult wipes it from tellStopped so a subsequent
    resume_all won't re-add it. Missing GID is a noop — the caller
    is responsible for rmtree'ing the wrapper folder itself."""
    for gid in _find_gids_for_wrapper(wrapper_name):
        try:
            _rpc("aria2.forceRemove", gid)
        except RuntimeError as exc:
            # "GID not found" if it's already stopped; that's fine.
            print(f"[aria2c] forceRemove {gid[:12]} skipped: {exc}", flush=True)
        try:
            _rpc("aria2.removeDownloadResult", gid)
        except RuntimeError:
            pass


def resume_all() -> None:
    """Scan wrapper folders for `.torrent` files, add any not already
    known to aria2. Handles first boot after refactor (session file
    missing) AND recovery from unclean shutdown (session file stale).

    Only resumes wrappers with a live `.aria2` control file — i.e.
    downloads aria2 was still working on when we last stopped. A
    wrapper with just a `.torrent` file (no `.aria2`) is either a
    completed old download whose seed session ended long ago, or a
    wrapper the user manually cleaned files out of. Re-adding either
    would kick off a hash-check and potentially a re-download of
    missing pieces — surprising the user with old torrents suddenly
    active again. Matches the spawn-per-magnet era behavior.
    """
    if not BT_LIBRARY.exists():
        return
    known = _known_infohashes()
    for wrapper in BT_LIBRARY.iterdir():
        if not wrapper.is_dir():
            continue
        if not any(wrapper.rglob("*.aria2")):
            continue  # completed or dormant — don't wake it up
        torrent_file = next(iter(wrapper.glob("*.torrent")), None)
        if torrent_file is None:
            continue
        try:
            data = torrent_file.read_bytes()
            info_hash = _infohash_from_torrent(data)
        except Exception as exc:
            print(f"[aria2c] resume: skipping {wrapper.name} — "
                  f"cannot read .torrent: {exc}", flush=True)
            continue
        if info_hash in known:
            continue
        # aria2.addTorrent wants the raw bencoded bytes, base64-encoded.
        b64 = base64.b64encode(data).decode("ascii")
        try:
            _rpc("aria2.addTorrent", b64, [], {"dir": str(wrapper)})
            print(f"[aria2c] resumed {wrapper.name}", flush=True)
        except Exception as exc:
            print(f"[aria2c] resume {wrapper.name} failed: {exc}", flush=True)


# ── Progress + control-file parsing (unchanged from old module) ───────────

def read_progress(wrapper: Path) -> tuple[int, int] | None:
    """Legacy fallback — aria2 RPC tellStatus is the primary progress
    source now. This function is only useful if aria2 doesn't know
    about the wrapper (orphaned state)."""
    ctl = next(iter(wrapper.rglob("*.aria2")), None)
    if ctl is None:
        return None
    try:
        data = ctl.read_bytes()
        off = 0
        _version = struct.unpack_from(">H", data, off)[0]; off += 2
        off += 4
        ihash_len = struct.unpack_from(">I", data, off)[0]; off += 4
        off += ihash_len
        piece_len = struct.unpack_from(">I", data, off)[0]; off += 4
        total_len = struct.unpack_from(">Q", data, off)[0]; off += 8
        off += 8
        bf_len = struct.unpack_from(">I", data, off)[0]; off += 4
        bitfield = data[off:off + bf_len]
    except (struct.error, OSError, IndexError):
        return None
    completed_pieces = sum(bin(b).count("1") for b in bitfield)
    downloaded = min(completed_pieces * piece_len, total_len)
    return (downloaded, total_len)


# ── Probe (metadata-only fetch, unchanged bencode parsing) ────────────────

PROBE_TIMEOUT_SEC = 150


def _bdecode(data: bytes, pos: int = 0):
    c = data[pos:pos + 1]
    if c == b"d":
        result: dict = {}
        pos += 1
        while data[pos:pos + 1] != b"e":
            key, pos = _bdecode(data, pos)
            value, pos = _bdecode(data, pos)
            result[key] = value
        return result, pos + 1
    if c == b"l":
        arr: list = []
        pos += 1
        while data[pos:pos + 1] != b"e":
            value, pos = _bdecode(data, pos)
            arr.append(value)
        return arr, pos + 1
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    start = colon + 1
    return data[start:start + length], start + length


def _bencode(value: Any) -> bytes:
    """Minimal bencoder — just enough to re-encode a `.torrent`'s
    `info` dict for infohash computation. Keys are bytes (from _bdecode),
    values are dict / list / int / bytes."""
    if isinstance(value, dict):
        out = b"d"
        for k in sorted(value.keys()):
            out += _bencode(k) + _bencode(value[k])
        return out + b"e"
    if isinstance(value, list):
        out = b"l"
        for v in value:
            out += _bencode(v)
        return out + b"e"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    raise TypeError(f"unbencodable type: {type(value).__name__}")


def _infohash_from_torrent(data: bytes) -> str:
    """SHA-1 hex of bencode(info) — the canonical BT infohash."""
    top, _ = _bdecode(data)
    info = top.get(b"info")
    if info is None:
        raise ValueError(".torrent has no info dict")
    return hashlib.sha1(_bencode(info)).hexdigest()


def _torrent_info(data: bytes) -> dict:
    d, _ = _bdecode(data)
    info = d.get(b"info") or {}
    name = info.get(b"name", b"").decode("utf-8", errors="replace")
    length = info.get(b"length")
    if length is not None:
        return {"size_bytes": int(length), "name": name}
    files = info.get(b"files") or []
    total = sum(int((f or {}).get(b"length") or 0) for f in files)
    return {"size_bytes": total, "name": name}


def probe(magnet: str, timeout_sec: int = PROBE_TIMEOUT_SEC) -> dict:
    """Fetch metadata WITHOUT downloading payload. Spawns a short-lived
    aria2c subprocess (NOT the seed daemon) with --bt-metadata-only, polls
    for the .torrent file to appear in a scratch dir, then kills the
    subprocess and cleans up.

    Why a subprocess and not the daemon's RPC: `bt-metadata-only` is a
    startup mode flag in aria2 (changes the whole process's lifecycle:
    stop after metadata resolution, don't enter payload-download phase).
    It's NOT a per-download option — passing it as a per-URI addUri
    option gets HTTP 400 from the RPC layer (aria2 can't have "some
    downloads in metadata-only mode, others normal" in the same daemon).
    A pause=true workaround pauses the metadata fetch itself, so no
    .torrent gets written. Subprocess is the only reliable path.

    Cold DHT bootstrap in a fresh aria2c can take 60-90s (empirically
    verified on Toy Story 5 magnet — 80s to find 23 peers via DHT). The
    150s default timeout gives real magnets room to complete; magnets
    with no live peers will still time out cleanly.

    Aria2 default BT/DHT port ranges (6881-6999) are shared with the
    seed daemon — aria2 tries the range in sequence and picks any free
    port, so no explicit --listen-port needed. Verified: daemon binding
    6881 doesn't stop subprocess from binding 6888.

    Buffered stderr is included in the raised TimeoutError so callers
    see why aria2c gave up — bad tracker set, DHT unreachable, invalid
    magnet, etc."""
    scratch = Path(tempfile.mkdtemp(prefix="probe-", dir="/tmp"))
    stderr_chunks: list[str] = []
    proc: subprocess.Popen | None = None
    try:
        cmd = [
            "aria2c",
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            "--seed-time=0",
            f"--dir={scratch}",
            "--enable-color=false",
            "--console-log-level=warn",
            "--summary-interval=0",
            magnet,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        def _drain() -> None:
            assert proc is not None and proc.stderr is not None
            for raw in proc.stderr:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    stderr_chunks.append(text)

        threading.Thread(target=_drain, daemon=True).start()

        deadline = time.time() + timeout_sec
        torrent_file: Path | None = None
        while time.time() < deadline:
            time.sleep(0.5)
            found = next(iter(scratch.rglob("*.torrent")), None)
            if found is not None and found.stat().st_size > 0:
                torrent_file = found
                break
            if proc.poll() is not None:
                # aria2c exited on its own — give the .torrent one last
                # chance to appear in case the file write races the exit.
                found = next(iter(scratch.rglob("*.torrent")), None)
                if found is not None and found.stat().st_size > 0:
                    torrent_file = found
                break

        if torrent_file is None:
            tail = "\n".join(stderr_chunks[-10:]) or "(no stderr — timeout waiting for peers)"
            raise TimeoutError(
                f"no .torrent produced within {timeout_sec}s — aria2c stderr:\n{tail}"
            )
        return _torrent_info(torrent_file.read_bytes())
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        shutil.rmtree(scratch, ignore_errors=True)


def shutdown() -> None:
    """Kept for API compatibility — the actual daemon shutdown is
    handled by stop_daemon(). Callers that only want the graceful
    aria2 shutdown (without waiting on the subprocess) can still
    use this; it's a noop if RPC is already dead."""
    try:
        _rpc("aria2.shutdown")
    except Exception:
        pass
