"""One-shot aria2c subprocess per magnet.

Design contract:
  * No daemon, no RPC, no shared session state. Every magnet submission
    is a fresh `aria2c` subprocess that lives just long enough to download
    + seed under our limits, then exits.
  * State is read from two places only — the filesystem under `/data/bt`
    (the per-torrent wrapper folders + aria2c's `.aria2` control file)
    and an in-memory dict of running subprocesses. Container restart
    clears the in-memory dict and kills the subprocesses; users notice
    via UI rather than state corruption.
  * Each torrent lands in its own per-torrent wrapper folder under
    `/data/bt` regardless of single- vs multi-file, derived from the
    magnet's `dn=` parameter. DELETE always rmtree's the wrapper.
  * Aria2c binds an ephemeral port in its default range (6881-6999).
    Since Surfshark doesn't offer port forwarding, incoming peer
    connections can't reach us; seeding relies on peers we handshaked
    with during download. Ratio rarely hits SEED_RATIO=1.0, so
    subprocesses typically terminate at SEED_TIME_MIN=1440 (24h)
    instead. Switching to a PF-supporting VPN (PIA / Proton / AirVPN)
    would need a `--listen-port=<forwarded_port>` here and a way to
    surface gluetun's forwarded_port file to this container.

Why no `.sh` on-bt-download-complete hook: aria2c writes a
`<file>.aria2` control file while a download is in flight and deletes it
the moment all pieces are verified. That's a clean filesystem-level
"download done" signal we can read directly — no callback plumbing.
"""
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BT_LIBRARY = Path("/data/bt")

# Seed limits applied to every torrent. aria2c exits when either limit is hit.
SEED_TIME_MIN = 1440
SEED_RATIO = 1.0

# Per-aria2c-process upload cap. We run one aria2c subprocess per torrent
# (not a shared daemon), so this is effectively a per-torrent cap — the
# global ceiling is SEED_UPLOAD_LIMIT × concurrent_seeders, not a true
# hard cap. Set conservatively (500K = ~4 Mbps) so 10 simultaneous
# seeders top out around 40 Mbps and leave uplink headroom for Jellyfin
# traffic sharing the same host NIC. Tune via env var; a real hard cap
# would need tc-based shaping on gluetun's tun interface, which we
# explicitly chose against for robustness (tunnel restarts wipe tc
# rules, interface names aren't API-stable).
SEED_UPLOAD_LIMIT = os.environ.get("SEED_UPLOAD_LIMIT", "500K")

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# wrapper folder name → live Popen handle. Used by `list_torrents` to know
# which wrappers still have a running aria2c, and by `delete_torrent` to
# kill it. Restart wipes this map (and kills the subprocesses with the
# parent container, since they're our children).
_procs: dict[str, subprocess.Popen] = {}
_procs_lock = threading.Lock()


# ── Wrapper folder naming ──────────────────────────────────────────────────

def _magnet_display_name(magnet: str) -> str | None:
    """Pull the `dn=` parameter from a magnet URI, URL-decoded."""
    try:
        u = urlparse(magnet)
        dn = parse_qs(u.query).get("dn", [None])[0]
        if dn:
            return unquote(dn)
    except Exception:
        pass
    return None


def _safe_folder(name: str) -> str:
    safe = _UNSAFE_NAME.sub("_", name).strip(" .\t\n")
    return safe[:180] or "torrent"


def _pick_wrapper_dir(magnet: str) -> Path:
    """Pick a free per-torrent wrapper folder under /data/bt for this magnet.

    Always puts the download inside its own folder so single-file torrents
    don't dump their .mkv + sidecar SRT loose at /data/bt root. Disambiguates
    against an existing folder with the same dn= by appending ` (2)`,
    ` (3)`, ... in case the user re-submits the same magnet or two
    different torrents share a dn=.
    """
    base = _safe_folder(_magnet_display_name(magnet) or "torrent")
    candidate = BT_LIBRARY / base
    i = 2
    while candidate.exists():
        candidate = BT_LIBRARY / f"{base} ({i})"
        i += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


# ── Submit / list / delete ─────────────────────────────────────────────────

def _spawn(wrapper: Path, source: str) -> None:
    """Spawn an aria2c subprocess into `wrapper` for `source` (a magnet URI
    or a path to a .torrent file). Shared by submit (magnet) and
    resume_all (saved .torrent)."""
    cmd = [
        "aria2c",
        f"--dir={wrapper}",
        f"--seed-time={SEED_TIME_MIN}",
        f"--seed-ratio={SEED_RATIO}",
        f"--max-overall-upload-limit={SEED_UPLOAD_LIMIT}",
        "--bt-save-metadata=true",
        "--enable-color=false",
        "--console-log-level=warn",
        "--summary-interval=0",
        source,
    ]

    proc = subprocess.Popen(
        cmd,
        # stdout: aria2c's progress spam isn't worth surfacing in our logs.
        stdout=subprocess.DEVNULL,
        # stderr: aria2c writes real errors (dead trackers, hash mismatches,
        # "no peers" complaints) here. Pipe through a drain thread into
        # aria2-app's own log so a stuck / failed torrent is at least
        # visible to `docker logs`.
        stderr=subprocess.PIPE,
        # Detach from our stdin so a SIGHUP / pipe-close to the parent
        # doesn't propagate. Children still die on container SIGTERM
        # (default).
        stdin=subprocess.DEVNULL,
    )
    with _procs_lock:
        _procs[wrapper.name] = proc

    short = wrapper.name[:30]

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[aria2c {short}] {text}", flush=True)

    threading.Thread(target=_drain_stderr, daemon=True).start()


def submit(magnet: str) -> str:
    """Spawn an aria2c subprocess for this magnet. Returns wrapper name."""
    wrapper = _pick_wrapper_dir(magnet)
    print(f"[aria2c] launching for {wrapper.name}", flush=True)
    _spawn(wrapper, magnet)
    return wrapper.name


def resume_all() -> None:
    """Restart aria2c for every wrapper that has an in-flight `.aria2`
    control file but no live subprocess.

    Called during aria2-app's startup lifespan. Container restart
    killed the previous subprocesses (PID 1 of the container died, kernel
    cleaned up the namespace), but aria2c left two things behind: the
    partial output files + `<file>.aria2` control file (its own resume
    metadata) and a `<infohash>.torrent` from --bt-save-metadata=true.
    Together those let aria2c pick up exactly where it left off when we
    re-spawn it with the saved .torrent.

    Best-effort: a wrapper whose .torrent never finished writing (rare —
    metadata fetch is one of the first things aria2c does) is logged as
    skipped, no further action.
    """
    if not BT_LIBRARY.exists():
        return
    for wrapper in BT_LIBRARY.iterdir():
        if not wrapper.is_dir():
            continue
        if not any(wrapper.rglob("*.aria2")):
            continue  # nothing in flight in this wrapper
        with _procs_lock:
            if wrapper.name in _procs and _procs[wrapper.name].poll() is None:
                continue  # already have a live subprocess (shouldn't happen at boot)
        torrent_file = next(wrapper.glob("*.torrent"), None)
        if torrent_file is None:
            print(f"[aria2c] cannot resume {wrapper.name}: no .torrent metadata on disk", flush=True)
            continue
        print(f"[aria2c] resuming {wrapper.name} from {torrent_file.name}", flush=True)
        _spawn(wrapper, str(torrent_file))


def read_progress(wrapper: Path) -> tuple[int, int] | None:
    """Parse the `.aria2` control file to return `(downloaded_bytes,
    total_bytes)`. Returns None if no control file exists (torrent is not
    downloading — either done, seeding, or never started) or the file is
    unreadable / has an unexpected format.

    Reading .aria2 is the only accurate progress signal — aria2's default
    `--file-allocation=prealloc` reserves full target size upfront, so
    `du`/`ls` show 100% before the first byte is downloaded.

    Control file format (aria2 v1, stable since v1.36):
        2  version (big-endian uint16, currently always 1)
        4  extension flags
        4  infohash length N
        N  infohash bytes
        4  piece length (bytes per piece)
        8  total length (uint64 big-endian)
        8  upload length
        4  bitfield length M
        M  bitfield (1 bit per piece — set = piece verified complete)
        ...

    downloaded = popcount(bitfield) * piece_length, clamped to total_length
    (the last piece may be smaller than piece_length, and it's simpler to
    saturate at 100% than special-case the tail piece).
    """
    ctl = next(iter(wrapper.rglob("*.aria2")), None)
    if ctl is None:
        return None
    try:
        data = ctl.read_bytes()
        off = 0
        _version = struct.unpack_from(">H", data, off)[0]; off += 2
        off += 4  # extension
        ihash_len = struct.unpack_from(">I", data, off)[0]; off += 4
        off += ihash_len
        piece_len = struct.unpack_from(">I", data, off)[0]; off += 4
        total_len = struct.unpack_from(">Q", data, off)[0]; off += 8
        off += 8  # upload
        bf_len = struct.unpack_from(">I", data, off)[0]; off += 4
        bitfield = data[off:off + bf_len]
    except (struct.error, OSError, IndexError):
        return None
    completed_pieces = sum(bin(b).count("1") for b in bitfield)
    downloaded = min(completed_pieces * piece_len, total_len)
    return (downloaded, total_len)


def _phase(wrapper: Path, proc: subprocess.Popen | None) -> str:
    """Derive `downloading` / `seeding` / `done` / `orphaned` from disk + proc state.

    The `.aria2` control file is aria2c's own download-in-progress marker,
    written next to each output file and deleted when all pieces verify.
    So its presence/absence is the canonical "are we still downloading"
    signal — no hook script or RPC needed to read it.
    """
    if proc is None:
        # No subprocess registered (post-restart, or never had one).
        # The torrent's files are still on disk but nobody's seeding.
        return "orphaned"
    if proc.poll() is not None:
        # aria2c exited — seed limit met, or download failed early.
        return "done"
    if any(wrapper.rglob("*.aria2")):
        return "downloading"
    return "seeding"


def list_torrents() -> list[dict]:
    """One entry per wrapper folder under /data/bt."""
    out = []
    if not BT_LIBRARY.exists():
        return out
    with _procs_lock:
        procs_snapshot = dict(_procs)
        # Drop entries whose subprocess has exited AND whose wrapper no
        # longer exists — keeps the dict from growing forever.
        for name, proc in list(_procs.items()):
            if proc.poll() is not None and not (BT_LIBRARY / name).exists():
                _procs.pop(name, None)

    for wrapper in sorted(BT_LIBRARY.iterdir()):
        if not wrapper.is_dir():
            continue
        proc = procs_snapshot.get(wrapper.name)
        phase = _phase(wrapper, proc)
        row = {
            "name": wrapper.name,
            "phase": phase,
        }
        if phase == "downloading":
            progress = read_progress(wrapper)
            if progress is not None:
                row["progress"] = {
                    "completed": progress[0],
                    "total": progress[1],
                }
        out.append(row)
    return out


def delete(wrapper_name: str) -> None:
    """Kill the aria2c subprocess for this wrapper (if running).

    The wrapper folder rmtree is deliberately NOT done here — the
    caller (transcribe over the shared /data/bt bind mount, or the
    user via `rm -rf`) is responsible for that. Splitting kill from
    rmtree lets transcribe orchestrate cleanup atomically alongside
    its own canonical / _sources / sentinel unlink work, and means an
    aria2 outage doesn't leave orphaned wrapper dirs on disk that a
    healthy transcribe could have removed."""
    with _procs_lock:
        proc = _procs.pop(wrapper_name, None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


PROBE_TIMEOUT_SEC = 90


def _bdecode(data: bytes, pos: int = 0):
    """Minimal bencode decoder returning (value, next_pos). Enough to
    parse a .torrent's `info` block for name + size — not a general
    bencode library. Keys stay as bytes; caller uses b"info", b"length",
    etc."""
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
    # length-prefixed byte string:  "<n>:<bytes>"
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    start = colon + 1
    return data[start:start + length], start + length


def _torrent_info(data: bytes) -> dict:
    """Extract total size + display name from a .torrent's bencoded bytes."""
    d, _ = _bdecode(data)
    info = d.get(b"info") or {}
    name = info.get(b"name", b"").decode("utf-8", errors="replace")
    # Single-file torrent has info.length; multi-file has info.files[].length.
    length = info.get(b"length")
    if length is not None:
        return {"size_bytes": int(length), "name": name}
    files = info.get(b"files") or []
    total = sum(int((f or {}).get(b"length") or 0) for f in files)
    return {"size_bytes": total, "name": name}


def probe(magnet: str, timeout_sec: int = PROBE_TIMEOUT_SEC) -> dict:
    """Fetch the magnet's `.torrent` metadata WITHOUT downloading the
    payload files. Returns `{size_bytes, name}` on success. Raises
    `TimeoutError` (misleadingly-named for any "no .torrent produced"
    outcome) with a stderr tail from aria2c so the caller can see why.

    Under the hood: spawn an aria2c with `--bt-metadata-only=true
    --bt-save-metadata=true --seed-time=0` into a scratch dir, poll
    for the `.torrent` file, then kill + cleanup regardless of outcome.
    Metadata typically lands in 5-30 s from a healthy DHT / trackers.
    """
    # Scratch dir lives OUTSIDE BT_LIBRARY so the shared /data/bt mount
    # never gets a transient probe folder that transcribe's reconciler
    # could see. /tmp is container-local and gets cleaned automatically.
    scratch = Path(tempfile.mkdtemp(prefix="probe-", dir="/tmp"))
    # Buffer stderr in a background thread — aria2c can wedge on a full
    # pipe if we never drain it, and we want the tail available for the
    # error message when things go wrong.
    stderr_chunks: list[str] = []
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
            assert proc.stderr is not None
            for raw in proc.stderr:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    stderr_chunks.append(text)
                    print(f"[aria2c probe] {text}", flush=True)

        threading.Thread(target=_drain, daemon=True).start()

        deadline = time.time() + timeout_sec
        torrent_file: Path | None = None
        try:
            while time.time() < deadline:
                time.sleep(0.5)
                # Some aria2c builds tuck the .torrent into a subdir
                # (e.g. via download-file-side metadata); rglob covers
                # both direct and nested placement.
                found = next(iter(scratch.rglob("*.torrent")), None)
                if found is not None and found.stat().st_size > 0:
                    torrent_file = found
                    break
                # If aria2c already exited without producing a .torrent,
                # no point waiting further.
                if proc.poll() is not None and torrent_file is None:
                    break
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        if torrent_file is None:
            # Include stderr tail so the caller (and Telegram) sees the
            # actual reason instead of a generic "timeout" line.
            tail = " | ".join(stderr_chunks[-5:]) if stderr_chunks else "(no stderr)"
            raise TimeoutError(
                f"no .torrent produced within {timeout_sec}s; "
                f"aria2c rc={proc.returncode}; stderr tail: {tail}"
            )
        return _torrent_info(torrent_file.read_bytes())
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def shutdown() -> None:
    """Kill every still-running aria2c on lifespan teardown.

    Docker's SIGTERM would do this anyway via the process tree, but doing
    it explicitly lets us reap the children and surface log lines if any
    refused to die.
    """
    with _procs_lock:
        for name, proc in _procs.items():
            if proc.poll() is None:
                proc.terminate()
        for name, proc in _procs.items():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        _procs.clear()
