import functools
import os
import re
import shutil
import subprocess
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import yt_dlp

from annotate import annotate_srt
from bt_filter import (
    ARTIFACT_ROOT,
    BT_ROOT,
    PROCESSED_DIR,
    _sources_path,
)
from gpu_lock import gpu_lock
from srt_source import (
    annotate_failed_path,
    stamp_annotate_failed,
    stamp_whisper_failed,
    whisper_failed_path,
)
from subs_finder import find_candidate_hash, find_candidate_text
from subs_verifier import verify_against_whisper
from storage import get_job, upsert_job

DOWNLOADS_DIR = Path("/app/data/downloads")
WHISPER_URL = os.environ.get("WHISPER_URL", "http://whisper:8000")

DOWNLOAD_TIMEOUT = 60 * 60        # 1 hour
TRANSCRIBE_TIMEOUT = 4 * 60 * 60  # 4 hours
RESYNC_TIMEOUT = 5 * 60           # alass on a long movie tops out well under this

# Single worker — pipeline (whisper + verify + alass + annotation) runs
# end-to-end on one thread so per-job state mutations stay serial and the
# GPU lock is held only when whisper is actually running. Annotation
# (~1 min Sonnet pass) inlines after whisper for a ~5% throughput hit, in
# exchange for an atomic "canonical exists = fully done" invariant — no
# separate ANNOTATING executor, no marker checks.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe-worker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed(since_iso: str) -> float:
    start = datetime.fromisoformat(since_iso)
    return (datetime.now(timezone.utc) - start).total_seconds()


def _catch_unhandled(fn):
    @functools.wraps(fn)
    def wrapped(job_id, *args, **kwargs):
        try:
            return fn(job_id, *args, **kwargs)
        except Exception as exc:
            traceback.print_exc()
            _fail(job_id, f"Unhandled error: {exc}")
    return wrapped


def _is_job_deleted(job_id: str) -> bool:
    job = get_job(job_id)
    return not job or job["status"] == "DELETED"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` via a sibling tmp file + rename. The
    rename is atomic on POSIX (single inode swap), so any other reader
    (Jellyfin scan, Infuse browse) sees either the prior file or the new
    file — never a half-written intermediate. Used for canonical-SRT
    writes where partial state would confuse downstream consumers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def enumerate_playlist(url: str) -> list[dict]:
    """Return [{id, title, url}, ...] for a YouTube playlist URL.

    extract_flat=True walks the playlist index without downloading any video.
    Unavailable / deleted entries surface as None and are dropped.
    """
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        vid = e.get("id")
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": e.get("title") or "",
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    return out


@_catch_unhandled
def process_video(job_id: str, url: str):
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS", "DOWNLOADING", "TRANSCRIBING"):
        return

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    staging_base = DOWNLOADS_DIR / job_id   # UUID-named while download/transcribe in progress

    job["status"] = "DOWNLOADING"
    job["updated_at"] = _now()
    upsert_job(job)

    download_started = _now()

    def progress_hook(d):
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            raise Exception("Job cancelled")
        if _elapsed(download_started) > DOWNLOAD_TIMEOUT:
            raise Exception("Download timed out (1 hour limit)")

    ydl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]",
        "merge_output_format": "mp4",
        "outtmpl": str(staging_base) + ".%(ext)s",
        "progress_hooks": [progress_hook],
        "quiet": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", job_id)
    except Exception as e:
        _fail(job_id, str(e))
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    job["title"] = title
    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_transcription(job_id, str(staging_base) + ".mp4")


def _sanitize_title(title: str) -> str:
    """Strip filesystem-unsafe chars; cap length; fallback if empty."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
    safe = safe.strip('. \t\n')
    return safe[:180] or "untitled"


def _unique_basename(base: str) -> str:
    """Suffix `(2)`, `(3)`... if a name with this base already has files."""
    candidate = base
    i = 2
    while (DOWNLOADS_DIR / f"{candidate}.mp4").exists() or (DOWNLOADS_DIR / f"{candidate}.srt").exists():
        candidate = f"{base} ({i})"
        i += 1
    return candidate


def _run_whisper(job_id: str, media_path: Path, srt_path: Path):
    """POST media to the shared whisper service; write returned SRT to srt_path.

    Holds the cross-container GPU lock around the HTTP call so this consumer
    doesn't race marker-pipeline for VRAM. faster-whisper-server has its own
    internal queue for whisper-only contention.

    Pre-flight DELETED check; raises on HTTP / server error.
    """
    current = get_job(job_id)
    if not current or current["status"] == "DELETED":
        return

    with gpu_lock("transcribe-app", f"whisper:{job_id}"):
        # Re-check after acquiring the lock (could have been deleted while we waited).
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            return

        try:
            with open(media_path, "rb") as f:
                resp = requests.post(
                    f"{WHISPER_URL}/v1/audio/transcriptions",
                    files={"file": (media_path.name, f, "application/octet-stream")},
                    data={
                        "model": "whisper-1",  # ignored by fedirz; uses WHISPER__MODEL
                        "response_format": "srt",
                        "temperature": "0",
                        # silero VAD strips silence/music sections before whisper
                        # processes — kills the hallucination loops ("CastingWords",
                        # "Thank you" etc.) that whisper falls into on long files
                        # with extended non-speech audio (movies, podcasts with
                        # instrumental segments). condition_on_previous_text isn't
                        # exposed by fedirz, so VAD is the only knob we have.
                        "vad_filter": "true",
                    },
                    timeout=(10, TRANSCRIBE_TIMEOUT),
                )
        except requests.RequestException as e:
            raise RuntimeError(f"Whisper service call failed: {e}") from e

    if resp.status_code != 200:
        raise RuntimeError(f"Whisper service returned {resp.status_code}: {resp.text[:300]}")

    srt_text = resp.text
    if not srt_text.strip():
        raise RuntimeError("Whisper service returned empty SRT")

    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(srt_text, encoding="utf-8")


def _resync(video: Path, candidate_srt: Path, output_srt: Path) -> bool:
    """Run alass to align `candidate_srt` to `video`'s audio, writing the
    result to `output_srt`. Candidate stays untouched (it lives under
    `_sources/` and is re-read on every replay). Returns True on success.

    alass does piecewise drift detection — if `candidate_srt` has a
    cold-open / recap / outro segment that doesn't exist in the video,
    alass aligns each piece independently instead of forcing a single
    uniform offset (which is what made ffsubsync misalign on releases
    with different opening structures). 5-minute timeout is plenty.
    """
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["alass", str(video), str(candidate_srt), str(output_srt)],
            capture_output=True,
            timeout=RESYNC_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[alass] error for {video.name!r}: {e}", flush=True)
        return False
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-1:]
        print(f"[alass] rc={r.returncode} for {video.name!r}: {tail}", flush=True)
        return False
    if not output_srt.exists() or output_srt.stat().st_size == 0:
        return False
    print(f"[alass] resynced → {output_srt.name}", flush=True)
    return True


def _run_transcription(job_id: str, staging_mp4: str):
    """yt path: whisper a staging mp4, run annotation inline, then rename
    the mp4 + write the annotated SRT atomically at title-based final
    paths. No candidate verification step — YouTube has only whisper as
    a source, so the verified.srt tier under /artifact/_sources/ doesn't
    apply (yt files don't even live under /artifact)."""
    staging_path = Path(staging_mp4)
    staging_srt = staging_path.with_suffix(".srt")

    # ── Whisper ───────────────────────────────────────────────────────
    try:
        _run_whisper(job_id, staging_path, staging_srt)
    except Exception as exc:
        _fail(job_id, str(exc))
        return

    if _is_job_deleted(job_id):
        return

    # ── Decide final paths ───────────────────────────────────────────
    job = get_job(job_id)
    if not job:
        return
    base = _unique_basename(_sanitize_title(job["title"]))
    final_mp4 = DOWNLOADS_DIR / f"{base}.mp4"
    final_srt = DOWNLOADS_DIR / f"{base}.srt"

    # ── Annotation ────────────────────────────────────────────────────
    job["status"] = "ANNOTATING"
    job["updated_at"] = _now()
    upsert_job(job)

    annotation_error: str | None = None
    try:
        annotated = annotate_srt(
            staging_srt,
            is_cancelled=lambda: _is_job_deleted(job_id),
        )
    except Exception as exc:
        traceback.print_exc()
        annotation_error = f"Annotation failed: {exc}"
        annotated = None

    if _is_job_deleted(job_id):
        return

    # On annotation failure or cancellation that wasn't deletion, fall
    # back to the un-annotated whisper transcript so the user at least
    # gets a usable subtitle file — yt jobs are one-shot, throwing away
    # the download+transcribe work on a transient API failure is too
    # punitive.
    if annotated is None and annotation_error is None:
        # Treated as graceful cancellation — bail without writing.
        return
    if annotated is None:
        try:
            annotated = staging_srt.read_text(encoding="utf-8")
        except OSError as exc:
            _fail(job_id, f"Whisper SRT unreadable: {exc}")
            return

    # ── Promote staging → final atomically ────────────────────────────
    try:
        _atomic_write_text(final_srt, annotated)
    except OSError as exc:
        _fail(job_id, f"write final SRT failed: {exc}")
        return
    if staging_path.exists():
        try:
            staging_path.rename(final_mp4)
        except OSError as exc:
            _fail(job_id, f"rename mp4 failed: {exc}")
            return
    staging_srt.unlink(missing_ok=True)

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return
    job["status"] = "SUCCESS"
    job["basename"] = base
    job["annotation_error"] = annotation_error
    job["updated_at"] = _now()
    upsert_job(job)


# ── BT video pipeline ─────────────────────────────────────────────────────

# Candidate sources we try in order. Bundled-SRT comes first because it's
# from the same release as the video (zero drift before alass) and
# already passed the bt_filter Haiku preview test for English / dialogue.
# OS hash next (exact byte-hash match → likely same release). OS text
# last (drift-prone — different release entirely).
_CANDIDATE_TAGS = ("bundled", "opensubtitles-hash", "opensubtitles-text")


def _find_bt_wrapper(canonical_video: Path) -> Path | None:
    """Reverse-lookup the /bt/<wrapper>/ directory a canonical video was
    hardlinked from. Two strategies:

      1. Read the `_processed/<wrapper>.filtered` sentinels (their content
         is the canonical-path manifest bt_filter wrote). The sentinel's
         filename is the sanitized wrapper name.

      2. Fallback: walk /bt and find any file with matching inode (since
         canonical is a hardlink of the bt-side file). Slower but robust
         when sanitization changed the wrapper name on the sentinel side.
    """
    try:
        rel_str = str(canonical_video.relative_to(ARTIFACT_ROOT))
    except ValueError:
        return None

    if PROCESSED_DIR.exists():
        for sentinel in PROCESSED_DIR.glob("*.filtered"):
            try:
                text = sentinel.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if rel_str in text.splitlines():
                wrapper = BT_ROOT / sentinel.stem
                if wrapper.is_dir():
                    return wrapper
                # Sanitization changed the on-disk name — fall through
                # to inode lookup.
                break

    return _find_bt_wrapper_by_inode(canonical_video)


def _find_bt_wrapper_by_inode(canonical_video: Path) -> Path | None:
    """Walk /bt looking for a file with the same inode as `canonical_video`
    (they share an inode because /artifact is a hardlink of /bt). The
    found file's top-level wrapper dir is the answer. Falls back to None
    if no match (orphaned canonical, /bt cleaned up, etc.)."""
    try:
        target_inode = canonical_video.stat().st_ino
    except OSError:
        return None
    if not BT_ROOT.exists():
        return None
    for wrapper in BT_ROOT.iterdir():
        if not wrapper.is_dir():
            continue
        for entry in wrapper.rglob("*"):
            try:
                if entry.is_file() and entry.stat().st_ino == target_inode:
                    return wrapper
            except OSError:
                continue
    return None


def _pick_bundled(video: Path, dest: Path, whisper_src: Path) -> Path | None:
    """Scan the bt-side wrapper for `.srt` files and find one whose content
    matches the whisper transcript (via WER). First match (sorted by
    filename) wins, gets copied to `dest`, and is returned.

    Filename ordering matters for season packs — `01_English.srt` <
    `01_SDH.srt` alphabetically means the plain English track gets tried
    before SDH variants. Wrong-episode subs (E02's SRT in an E01 lookup)
    have very high WER and get rejected naturally.

    No bonus-dir exclusion: bonus content's SRTs have completely different
    dialogue from the main feature, so they fail WER without help.
    """
    wrapper = _find_bt_wrapper(video)
    if wrapper is None:
        return None

    for srt in sorted(wrapper.rglob("*.srt")):
        try:
            rel = srt.relative_to(wrapper)
        except ValueError:
            continue
        ok, reason = verify_against_whisper(whisper_src, srt)
        if not ok:
            print(f"[bundled-scan] {video.name!r}: REJECT {rel} — {reason}", flush=True)
            continue
        print(f"[bundled-scan] {video.name!r}: PICK {rel} — {reason}", flush=True)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(srt), str(dest))
        except OSError as exc:
            print(f"[bundled-scan] copy failed: {exc}", flush=True)
            return None
        return dest

    return None


def _fetch_candidate(tag: str, video: Path, dest: Path, whisper_src: Path) -> Path | None:
    """Materialize a candidate SRT at `dest` if we don't already have it.
    Returns `dest` on success, None on miss.

    Bundled candidates are discovered by scanning the bt-side wrapper
    and picking the first `.srt` whose content matches `whisper_src` —
    no bt_filter pre-staging, no filename heuristics."""
    if dest.exists():
        return dest

    if tag == "bundled":
        return _pick_bundled(video, dest, whisper_src)

    if tag == "opensubtitles-hash":
        return find_candidate_hash(video, "en", dest)

    if tag == "opensubtitles-text":
        return find_candidate_text(video, "en", dest)

    return None


@_catch_unhandled
def process_bt_file(job_id: str):
    """bt video pipeline — produces a canonical, annotated SRT through
    four cacheable stages, each writing its output to `_sources/` and
    only the final annotated text landing at the canonical path. Each
    stage skips if its output already exists on disk, so any partial
    progress survives crashes / restarts / manual deletions:

      1. whisper      → /artifact/_sources/.../<stem>.whisper.srt
      2. candidates   → /artifact/_sources/.../<stem>.{bundled,opensubtitles-*}.srt
      3. verify+sync  → /artifact/_sources/.../<stem>.verified.srt
                        (winner from step 2 → alass; or whisper itself
                         if no candidate passed the WER gate)
      4. annotate     → /artifact/.../<stem>.srt   (atomic mv tmp → canonical)

    State recovery on restart: a pipeline that died mid-run leaves
    whatever it managed to write under `_sources/` intact. The next
    scan tick re-queues `process_bt_file`, which picks up at the first
    stage whose output is missing — no manual intervention, no marker
    reads, no jobs.json overlay.

    Whisper failure → `<stem>.whisper-failed` sidecar, halt; canonical
    never written, _sources/ left as-is (might be empty).

    Annotation failure → `<stem>.annotate-failed` sidecar, halt;
    canonical never written, `_sources/<stem>.verified.srt` retained so
    ↻ replay only re-runs annotation.
    """
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS"):
        return

    source_path = job.get("source_path")
    if not source_path:
        _fail(job_id, "bt job missing source_path")
        return

    video = Path(source_path)
    if not video.exists():
        _fail(job_id, f"Source file missing: {source_path}")
        return

    canonical = video.with_suffix(".srt")
    whisper_src = _sources_path(video, "whisper")
    verified_src = _sources_path(video, "verified")

    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    # ── 1. Whisper (cached) ───────────────────────────────────────────
    if not whisper_src.exists():
        try:
            _run_whisper(job_id, video, whisper_src)
        except Exception as exc:
            try:
                stamp_whisper_failed(video, str(exc))
            except OSError:
                pass
            _fail(job_id, str(exc))
            return
    else:
        print(f"[pipeline] reusing cached whisper SRT for {video.name!r}", flush=True)

    if _is_job_deleted(job_id):
        return

    # ── 2 & 3. Verified.srt (cached) ──────────────────────────────────
    if not verified_src.exists():
        winner_tag: str | None = None
        for tag in _CANDIDATE_TAGS:
            cand_dest = _sources_path(video, tag)
            cand_path = _fetch_candidate(tag, video, cand_dest, whisper_src)
            if cand_path is None:
                continue

            ok, reason = verify_against_whisper(whisper_src, cand_path)
            if not ok:
                print(f"[pipeline] {video.name!r}: {tag} REJECT — {reason}", flush=True)
                continue
            print(f"[pipeline] {video.name!r}: {tag} ACCEPT — {reason}", flush=True)

            if _resync(video, cand_path, verified_src):
                winner_tag = tag
                break
            # alass failed — try next candidate. Un-synced candidate
            # isn't worth using; timing drift over a 50-min episode is
            # worse than whisper output.
            print(f"[pipeline] {video.name!r}: {tag} alass failed; trying next", flush=True)

        if winner_tag is None:
            # No candidate passed. Whisper IS the verified output (it's
            # already audio-aligned, no alass needed).
            try:
                verified_src.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(whisper_src), str(verified_src))
                print(f"[pipeline] {video.name!r}: no candidate verified → whisper promoted to verified.srt", flush=True)
            except OSError as exc:
                _fail(job_id, f"copy whisper → verified failed: {exc}")
                return
    else:
        print(f"[pipeline] reusing cached verified SRT for {video.name!r}", flush=True)

    if _is_job_deleted(job_id):
        return

    # ── 4. Annotation → canonical (atomic) ────────────────────────────
    job["status"] = "ANNOTATING"
    job["updated_at"] = _now()
    upsert_job(job)

    try:
        annotated = annotate_srt(
            verified_src,
            is_cancelled=lambda: _is_job_deleted(job_id),
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            stamp_annotate_failed(video, str(exc))
        except OSError:
            pass
        _fail(job_id, f"Annotation failed: {exc}")
        return

    if annotated is None:
        # Cancelled (job DELETED). Nothing written to canonical; the
        # next-time replay re-runs only the annotation step thanks to
        # the cached verified.srt.
        return

    try:
        _atomic_write_text(canonical, annotated)
    except OSError as exc:
        _fail(job_id, f"write canonical failed: {exc}")
        return

    # Successfully produced the canonical SRT — clear any stale failure
    # sidecars from a prior run so the file isn't accidentally skipped.
    for sidecar in (whisper_failed_path(video), annotate_failed_path(video)):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return
    job["status"] = "SUCCESS"
    job["updated_at"] = _now()
    upsert_job(job)


def _fail(job_id: str, error: str):
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "FAILED"
    job["error"] = error
    job["updated_at"] = _now()
    upsert_job(job)
