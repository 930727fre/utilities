import functools
import os
import re
import shutil
import subprocess
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

from annotate import annotate_srt
from archive import mirror_to_archive
from bt_filter import _sources_path
from bundled import pick_bundled_min_wer, pick_bundled_smart_plot
from container_subs import extract_embedded, extract_pgs_ocr, extract_vobsub_ocr
from os_tier import fetch_os_ktry, os_ktry
from srt_source import (
    annotate_failed_path,
    stamp_annotate_failed,
    stamp_whisper_failed,
    stamp_whisper_polluted,
    whisper_failed_path,
    whisper_polluted_path,
)
from subs_finder import _parse_filename
from subs_verifier import (
    cue_count_ok,
    find_pollution_windows,
    pollution_cue_ratio,
    verify_by_plot,
)
from storage import get_job, upsert_job
from whisper_client import run_whisper

DOWNLOADS_DIR = Path("/app/data/downloads")

DOWNLOAD_TIMEOUT = 60 * 60         # 1 hour
RESYNC_TIMEOUT = 5 * 60            # alass on a long movie tops out well under this

# If more than this fraction of whisper's cues fall inside hallucination
# windows, whisper is too degraded to serve as a WER reference — any
# full-length candidate scores noise-dominated. Bail straight to the
# plot-check fallback loop (LLM decides content match directly); if that
# fails too, stamp `.whisper-polluted` for user intervention.
#
# Cue-based rather than time-based: a movie can have 20% of its runtime
# eaten by "Hey." loops in silent scenes and still test "under
# threshold" on a time-coverage metric, while the whisper transcript is
# 80% pollution and effectively useless as a WER reference. The
# Victoria (2015) case-in-point: 27% time coverage, 78% polluted-cue
# ratio — the cue ratio catches it, time coverage lets it slip.
_POLLUTION_CUE_RATIO_BAIL = 0.5

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
        run_whisper(staging_path, staging_srt,
                    is_cancelled=lambda: _is_job_deleted(job_id),
                    lock_reason=f"whisper:{job_id}")
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

# Candidate sources we try in order, from "same source as the video" to
# "different release entirely":
#
#   embedded           — SubRip track inside the mkv container itself.
#                        Authored against the same master as the video,
#                        timing is byte-perfect; alass is skipped for this
#                        tag. Common in BluRay / WEB-DL rips (PSA, NTb,
#                        FLUX...). When present this is unambiguously the
#                        best source — no community / OS roulette.
#   pgs-ocr            — PGS (bitmap) subtitle track from the mkv, OCR'd
#                        to SRT via pgsrip → tesseract. Same-source so
#                        timing is byte-perfect (alass skipped), but
#                        text quality is ~95% character accuracy at
#                        best — italics / music / em-dash glyphs OCR
#                        worse. Covers releases that ship PGS only
#                        (Chernobyl + many HBO Blu-rays).
#   bundled            — .srt sidecar in the bt-side wrapper. Usually from
#                        the same release, may need minor alignment.
#   opensubtitles-hash — exact video-hash match on OpenSubtitles. Quality
#                        is correlated with whether the release ships its
#                        own subs: releases with embedded SRT attract few
#                        OS uploads, so OS-hash for those is statistically
#                        junk (some uploader mis-tagged a different cut's
#                        sub against this hash). Hence embedded-first
#                        ordering above.
#   opensubtitles-text — text-search fallback. Drift-prone (different
#                        release entirely) and metadata can lie (wrong
#                        S/E in the file we get back). WER catches those.
# "archive" is tried first so a previously-canonical SRT that survived a
# delete_torrent + re-download cycle is reused without paying whisper /
# Sonnet / Gemini costs again. See archive.py for the mirror + lookup rules.
#
# Split by whether the tier needs whisper as reference. Whisper-
# independent tiers are attempted BEFORE whisper — if any hits, we skip
# the 10-30 minute GPU pass entirely. Same-source container extractions
# (embedded / pgs-ocr / vobsub-ocr from the mkv itself) don't need a
# whisper ASR reference; content is guaranteed by construction.
# (Archive-tier attach happens earlier, inside bt_filter.filter_wrapper,
# and lands the SRT directly at canonical — those videos never reach
# process_bt_file because `has_srt=True` filters them out at scan time.)
# Only the "external release" tiers (bundled / OS) need whisper as
# content-match reference (normal WER) or pollution indicator
# (polluted-mode plot-check dispatch).
_WHISPER_INDEPENDENT_TAGS = ("embedded", "pgs-ocr", "vobsub-ocr")
_WHISPER_DEPENDENT_TAGS = ("bundled", "opensubtitles-hash", "opensubtitles-text")

def _fetch_candidate(
    tag: str,
    video: Path,
    dest: Path,
    whisper_src: Path,
) -> Path | None:
    """Materialize a candidate SRT at `dest` if we don't already have it.
    Returns `dest` on success, None on miss."""
    if dest.exists():
        return dest

    if tag == "embedded":
        return extract_embedded(video, dest)

    if tag == "pgs-ocr":
        return extract_pgs_ocr(video, dest)

    if tag == "vobsub-ocr":
        return extract_vobsub_ocr(video, dest)

    if tag == "bundled":
        # Normal mode: score every wrapper sub against whisper, pick
        # min-WER, gate at WER_PASS_MAX. Best-of-all avoids the
        # ordering pitfall where a wrong-episode srt in a season
        # pack lands just under threshold before the correct one
        # is tried.
        return pick_bundled_min_wer(video, dest, whisper_src)

    if tag == "opensubtitles-hash":
        return fetch_os_ktry(video, dest, whisper_src, mode="hash")

    if tag == "opensubtitles-text":
        return fetch_os_ktry(video, dest, whisper_src, mode="text")

    return None


def _try_whisper_independent_tiers(video: Path, verified_src: Path) -> str | None:
    """Try archive → embedded → pgs-ocr in order. These tiers don't
    need whisper as a reference (archive is prior-verified past
    pipeline output; embedded / pgs-ocr are same-source content
    extracted from the mkv container itself), so calling this before
    whisper lets a hit skip the 10-30 minute GPU pass entirely.

    Cue-count filter drops broken extractions (partial tracks, OCR
    complete failures) cheaply. Winner is copied verbatim to
    `verified_src` — no alass (archive was aligned in its prior run,
    embedded / pgs-ocr share the video's timeline by construction).

    Returns winning tag or None if all three miss."""
    for tag in _WHISPER_INDEPENDENT_TAGS:
        cand_dest = _sources_path(video, tag)
        # whisper_src is unused by these fetch paths.
        cand_path = _fetch_candidate(tag, video, cand_dest,
                                     whisper_src=Path("/dev/null"))
        if cand_path is None:
            continue

        # Archive is prior-verified past canonical, guaranteed to have
        # been full-dialogue length when it was written; skip the
        # redundant re-check. Embedded / pgs-ocr can produce short output
        # (forced subs, OCR bomb) that the check catches.
        if tag != "archive" and not cue_count_ok(cand_path, cand_path.name,
                                                 video, tag=tag):
            continue

        try:
            verified_src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(cand_path), str(verified_src))
        except OSError as exc:
            print(f"[pipeline] {video.name!r}: {tag} → verified copy failed: {exc}", flush=True)
            continue
        print(f"[pipeline] {video.name!r}: {tag} → verified.srt "
              f"(TRUST — same-source / prior-verified, whisper skipped)",
              flush=True)
        return tag
    return None


def _polluted_fallback_pick_candidate(
    video: Path,
    whisper_src: Path,
    verified_src: Path,
) -> str | None:
    """Coverage-bail fallback: whisper is >50% polluted, so WER can't
    discriminate any candidate. Reach for the whisper-dependent tiers
    with plot-check as the accept signal instead.

    Only called after the whisper-independent tiers (archive / embedded
    / pgs-ocr) already missed — those are tried before whisper runs at
    all, so by the time this fires they've been ruled out and we're
    only reaching for bundled + OS.

    - `bundled`: LLM smart-pick narrows the wrapper's srts to one by
      filename convention; `verify_by_plot` verifies content. Bounds
      cost to one Haiku pick + one Opus plot-check per attempt.
    - `opensubtitles-hash` / `opensubtitles-text`: k-try, but the accept
      callback is `verify_by_plot` (Opus + web_search) instead of WER.
    alass aligns the winner to the video's audio for all three.

    Returns the winning tag or None. On success, `verified_src` has
    been written."""
    info = _parse_filename(video)
    show = info.get("title") or ""
    season = info.get("season")
    episode = info.get("episode")
    if not show:
        print(f"[pipeline] {video.name!r}: polluted-mode — could not parse show "
              f"name from filename, plot-check impossible", flush=True)
        return None

    ep_ref = f" S{season:02d}E{episode:02d}" if season is not None and episode is not None else ""
    print(f"[pipeline] {video.name!r}: polluted-mode candidate loop — target="
          f"{show!r}{ep_ref}", flush=True)

    def _plot_accept_local(cand: Path) -> tuple[bool, str]:
        return verify_by_plot(cand, show, season, episode)

    for tag in _WHISPER_DEPENDENT_TAGS:
        cand_dest = _sources_path(video, tag)

        if tag == "bundled":
            def _plot_accept_bundled(cand: Path) -> bool:
                ok, reason = _plot_accept_local(cand)
                print(f"[pipeline] {video.name!r}: polluted-mode bundled "
                      f"plot-check {cand.name} — {reason}", flush=True)
                return ok
            cand_path = pick_bundled_smart_plot(video, cand_dest, _plot_accept_bundled)
            if cand_path is None:
                continue
            if _resync(video, cand_path, verified_src):
                return tag
            print(f"[pipeline] {video.name!r}: polluted-mode bundled alass failed", flush=True)
            continue

        # OS tiers: k-try with plot-check
        mode = "hash" if tag == "opensubtitles-hash" else "text"

        def _plot_accept_os(cand: Path) -> bool:
            ok, reason = _plot_accept_local(cand)
            print(f"[pipeline] {video.name!r}: polluted-mode {mode} plot-check "
                  f"{cand.name} — {reason}", flush=True)
            return ok

        winner = os_ktry(video, cand_dest, mode, _plot_accept_os)
        if winner is None:
            continue

        if _resync(video, winner, verified_src):
            return tag
        print(f"[pipeline] {video.name!r}: polluted-mode {tag} alass failed", flush=True)

    return None


@_catch_unhandled
def process_bt_file(job_id: str):
    """bt video pipeline — produces a canonical, annotated SRT through
    a two-stage source-selection process, each writing to `_sources/`
    and only the final annotated text landing at the canonical path.

      Stage A. Whisper-independent tiers (fast, no GPU) — try in order:
                 archive     → /artifact/_sources/.../<stem>.archive.srt
                 embedded    → /artifact/_sources/.../<stem>.embedded.srt
                 pgs-ocr     → /artifact/_sources/.../<stem>.pgs-ocr.srt
               Any hit → copy verbatim to verified.srt (no alass — timing
               is same-source or already-aligned), skip Stage B entirely.
      Stage B. Whisper + whisper-dependent tiers (only on Stage A miss):
                 whisper     → <stem>.whisper.srt (GPU 10-30min)
                 bundled     → <stem>.bundled.srt (WER inside pick, or
                               polluted-mode smart-pick + plot-check)
                 os-hash     → <stem>.opensubtitles-hash-N.srt
                 os-text     → <stem>.opensubtitles-text-N.srt
                 (bundled + os need whisper as WER reference / pollution
                 dispatch signal — hence Stage B ordering)
                 Winner → alass → verified.srt.
                 All miss + clean whisper → whisper itself promoted to
                 verified.srt. All miss + polluted → `.whisper-polluted`
                 sidecar, halt.
      Final.  annotate → /artifact/.../<stem>.srt (atomic mv tmp → canonical)
               Skipped for archive wins — the archive.srt is already an
               annotated canonical from a prior run.

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

    winner_tag: str | None = None

    # ── Stage A. Whisper-independent tiers ────────────────────────────
    # archive / embedded / pgs-ocr don't need whisper as a reference
    # (prior-verified content or same-source extraction). Try them
    # first — a hit lets us skip the 10-30 min GPU pass entirely.
    if not verified_src.exists():
        winner_tag = _try_whisper_independent_tiers(video, verified_src)

    if _is_job_deleted(job_id):
        return

    # ── Stage B. Whisper + whisper-dependent tiers ────────────────────
    if winner_tag is None and not verified_src.exists():
        if not whisper_src.exists():
            try:
                run_whisper(video, whisper_src,
                            is_cancelled=lambda: _is_job_deleted(job_id),
                            lock_reason=f"whisper:{job_id}")
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

        # Detect whisper hallucination loops upfront. The windows are used
        # as a ROUTING signal only: if pollution_cue_ratio exceeds
        # _POLLUTION_CUE_RATIO_BAIL, whisper is too degraded to be a
        # useful WER reference — fall back to the plot-check loop
        # (bundled smart-pick + os plot-check); if that fails too, stamp
        # `.whisper-polluted`. Below the threshold, WER is computed on
        # raw whisper (no scrub — single-side scrub was empirically
        # harmful; see commit removing it).
        windows = find_pollution_windows(whisper_src)
        if windows:
            cue_ratio = pollution_cue_ratio(whisper_src, windows)
            print(f"[pipeline] {video.name!r}: whisper has {len(windows)} polluted "
                  f"window(s), {cue_ratio:.0%} of cues are pollution", flush=True)

            if cue_ratio > _POLLUTION_CUE_RATIO_BAIL:
                print(f"[pipeline] {video.name!r}: polluted cues > "
                      f"{_POLLUTION_CUE_RATIO_BAIL:.0%} — WER disabled; "
                      f"falling back to plot-check", flush=True)
                winner_tag = _polluted_fallback_pick_candidate(
                    video, whisper_src, verified_src,
                )
                if winner_tag is None:
                    polluted_reason = (
                        f"{cue_ratio:.0%} of whisper cues are hallucinations, "
                        f"no candidate passed plot-check fallback"
                    )
                    try:
                        stamp_whisper_polluted(video, polluted_reason)
                    except OSError:
                        pass
                    _fail(job_id, f"whisper polluted, no salvage possible ({polluted_reason})")
                    return
                if _is_job_deleted(job_id):
                    return

        # Normal WER candidate loop for whisper-dependent tiers — skip
        # when the polluted-fallback branch already wrote verified.srt.
        if winner_tag is None:
            for tag in _WHISPER_DEPENDENT_TAGS:
                cand_dest = _sources_path(video, tag)
                cand_path = _fetch_candidate(tag, video, cand_dest, whisper_src)
                if cand_path is None:
                    continue

                # Both bundled and os-* verify content INSIDE `_fetch_candidate`
                # (WER accept callback wired inline for bundled, iter_candidates
                # accept for OS). A non-None return already means the candidate
                # passed — no outer re-verify needed.
                print(f"[pipeline] {video.name!r}: {tag} ACCEPT "
                      f"(verified inside fetch)", flush=True)

                if _resync(video, cand_path, verified_src):
                    winner_tag = tag
                    break
                # alass failed — try next candidate. Un-synced candidate
                # isn't worth using; timing drift over a 50-min episode is
                # worse than whisper output.
                print(f"[pipeline] {video.name!r}: {tag} alass failed; trying next", flush=True)

            if winner_tag is None:
                if windows:
                    # Whisper had hallucination loops (below the cue-ratio
                    # bail, so scrub was attempted) but no candidate salvaged
                    # it — refuse to promote a partially-hallucinated whisper
                    # to canonical. Stamp the sidecar so the scan loop stops
                    # retrying and the UI surfaces the state for the user to
                    # intervene (drop a bundled SRT, refetch OS once quota
                    # recovers, etc.).
                    ratio = pollution_cue_ratio(whisper_src, windows)
                    polluted_reason = (
                        f"{len(windows)} polluted window(s), {ratio:.0%} of "
                        f"whisper cues affected, no candidate salvaged after "
                        f"whisper-side scrub"
                    )
                    try:
                        stamp_whisper_polluted(video, polluted_reason)
                    except OSError:
                        pass
                    _fail(job_id, f"whisper polluted, no usable candidate ({polluted_reason})")
                    return
                # Whisper is clean and no candidate passed — fallback to
                # whisper-as-verified (already audio-aligned, no alass).
                try:
                    verified_src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(whisper_src), str(verified_src))
                    print(f"[pipeline] {video.name!r}: no candidate verified → whisper promoted to verified.srt", flush=True)
                except OSError as exc:
                    _fail(job_id, f"copy whisper → verified failed: {exc}")
                    return
    elif verified_src.exists() and winner_tag is None:
        print(f"[pipeline] reusing cached verified SRT for {video.name!r}", flush=True)

    if _is_job_deleted(job_id):
        return

    # ── 4. Annotation → canonical (atomic) ────────────────────────────
    #
    # (Archive-attached videos never reach this function — bt_filter
    # copies archive SRT to canonical at wrapper-arrival time, and
    # `_scan_bt` filters them out via `has_srt=True`. So process_bt_file
    # always annotates + writes canonical + mirrors to archive.)
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

    # Mirror to data/archive/ so a future delete_torrent + re-download
    # cycle can reuse this SRT (bt_filter's archive-attach on next
    # wrapper arrival picks it up via direct string match).
    mirror_to_archive(canonical)

    # Successfully produced the canonical SRT — clear any stale failure
    # sidecars from a prior run so the file isn't accidentally skipped.
    for sidecar in (
        whisper_failed_path(video),
        whisper_polluted_path(video),
        annotate_failed_path(video),
    ):
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
