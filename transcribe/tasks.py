import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
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
MKVMERGE_PROBE_TIMEOUT = 30       # JSON track probe — milliseconds in practice
MKVEXTRACT_TIMEOUT = 60           # demux one SRT track — seconds on a movie-sized mkv
PGS_OCR_TIMEOUT = 10 * 60         # tesseract OCR of a 50-min episode's PGS — ~1-3 min typical
FFMPEG_AUDIO_TIMEOUT = 5 * 60     # transcoding a movie to 16kHz mono AAC — ~30-60s typical

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


def _extract_audio_for_whisper(media_path: Path, out_path: Path) -> None:
    """Transcode `media_path`'s audio to a 16 kHz mono AAC file at
    `out_path`. Raises on ffmpeg failure.

    Whisper's first internal step is exactly this same `-vn -ac 1
    -ar 16000` ffmpeg pass. Doing it client-side instead of letting
    whisper-server's ffmpeg do it server-side means the HTTP body we
    POST is ~30-50 MB regardless of source resolution / video bitrate
    / subtitle-track count, instead of the 1-3 GB the source mkv is.

    Concrete reason this matters: PSA Chernobyl Blu-rays are 2.3-2.5
    GB each (HEVC 1080p video + 11 PGS subtitle tracks). Uploading
    those over the docker bridge to whisper-server hits a sporadic
    "Connection aborted" — root cause unidentified but consistently
    correlated with file size (1.1 GB GoT episodes upload first-try,
    2.5 GB Chernobyl episodes need 3-4 retries). Pre-transcoding
    sidesteps the whole class by keeping uploads small + uniform.

    The output is `.m4a` (AAC in mp4 container). 64 kbps is plenty
    headroom for the 16 kHz mono signal whisper consumes; quality
    parity with letting whisper-server downsample server-side.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-loglevel", "error",
                "-i", str(media_path),
                "-vn",           # drop video stream
                "-ac", "1",      # mono — whisper downmixes anyway
                "-ar", "16000",  # 16 kHz — whisper's internal sample rate
                "-c:a", "aac",
                "-b:a", "64k",
                str(out_path),
            ],
            capture_output=True,
            timeout=FFMPEG_AUDIO_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        raise RuntimeError(f"ffmpeg audio extraction failed: {e}") from e
    if r.returncode != 0:
        stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"ffmpeg audio extraction returned {r.returncode}: {stderr[-300:]}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg audio extraction produced empty output")


def _run_whisper(job_id: str, media_path: Path, srt_path: Path):
    """Transcode `media_path` to a small audio file, POST it to the
    shared whisper service, write the returned SRT to `srt_path`.

    Holds the cross-container GPU lock around the HTTP call so this
    consumer doesn't race marker-pipeline for VRAM. faster-whisper-server
    has its own internal queue for whisper-only contention.

    Audio extraction runs OUTSIDE the GPU lock — it's pure CPU work
    (ffmpeg), no reason to block other GPU consumers while we transcode.
    See `_extract_audio_for_whisper` for why we extract client-side.

    Pre-flight DELETED check; raises on ffmpeg or HTTP / server error.
    """
    current = get_job(job_id)
    if not current or current["status"] == "DELETED":
        return

    with tempfile.TemporaryDirectory(prefix="whisper-audio-") as tmpdir:
        audio_path = Path(tmpdir) / "audio.m4a"
        _extract_audio_for_whisper(media_path, audio_path)

        if _is_job_deleted(job_id):
            return

        with gpu_lock("transcribe-app", f"whisper:{job_id}"):
            # Re-check after acquiring the lock (could have been deleted while we waited).
            current = get_job(job_id)
            if not current or current["status"] == "DELETED":
                return

            try:
                with open(audio_path, "rb") as f:
                    resp = requests.post(
                        f"{WHISPER_URL}/v1/audio/transcriptions",
                        files={"file": (audio_path.name, f, "audio/mp4")},
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
_CANDIDATE_TAGS = ("embedded", "pgs-ocr", "bundled", "opensubtitles-hash", "opensubtitles-text")


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


def _find_subtitle_track(video: Path, codec_match) -> tuple[int, str] | None:
    """Probe `video` (mkv) for the best subtitle track of a given codec
    family. Returns `(track_id, lang_label)` where lang_label is "eng"
    or "und" depending on which fallback tier matched. None if no track
    matches the predicate.

    `codec_match(codec_lowered: str) -> bool` decides which codec
    strings count — pass `lambda c: "subrip" in c or "srt" in c` for
    text SubRip tracks, `lambda c: "pgs" in c` for PGS image tracks.

    Tracks marked `forced_track: true` are skipped — those only
    translate non-dialogue visual elements (foreign signs, paper
    notes, on-screen text) and are useless against a whisper
    transcript that covers the full spoken dialogue. WER would catch
    a forced-track winner, but skipping at the probe layer avoids
    wasted OCR / extraction work and lets a sibling full track in
    the same container be picked instead.

    Two-pass selection: explicit eng/en tag wins; otherwise the first
    track tagged und/zxx/empty is taken as a content fallback (BluRay
    rips frequently ship English subs without language metadata). The
    WER gate downstream verifies the content is actually English.
    """
    try:
        probe = subprocess.run(
            ["mkvmerge", "-J", str(video)],
            capture_output=True,
            timeout=MKVMERGE_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[mkvmerge] probe failed for {video.name!r}: {e}", flush=True)
        return None
    if probe.returncode != 0:
        return None
    try:
        info = json.loads(probe.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    english_id: int | None = None
    fallback_id: int | None = None
    for track in info.get("tracks") or []:
        if track.get("type") != "subtitles":
            continue
        codec = str(track.get("codec") or "").lower()
        if not codec_match(codec):
            continue
        props = track.get("properties") or {}
        # Skip forced subtitle tracks — they only translate non-dialogue
        # visual elements (foreign signs, paper notes, on-screen text)
        # and produce 10-30 cues per episode of fragments that look like
        # subtitle content but cover none of the actual spoken dialogue.
        # WER would reject them downstream anyway, but skipping at the
        # probe layer avoids a wasted mkvextract + tesseract pass and
        # lets us prefer a sibling full track in the same container.
        # Concrete failure mode: HBO Chernobyl muxes a forced English
        # PGS track BEFORE the full English PGS track; without this
        # skip we'd OCR the forced one and get 12 cues per episode.
        if props.get("forced_track"):
            continue
        lang = str(props.get("language") or "").lower()
        track_id = track.get("id")
        if track_id is None:
            continue
        if lang in ("eng", "en") and english_id is None:
            english_id = track_id
        elif lang in ("", "und", "zxx") and fallback_id is None:
            fallback_id = track_id

    if english_id is not None:
        return english_id, "eng"
    if fallback_id is not None:
        return fallback_id, "und"
    return None


def _extract_embedded(video: Path, dest: Path) -> Path | None:
    """Extract the first usable English SubRip track from the video's mkv
    container to `dest`. Returns dest on success, None if no extractable
    track exists.

    Same-source extraction: the subtitle stream was authored against the
    same master as the video, so timing is exact — the pipeline skips
    alass for this candidate. PGS / VobSub tracks are NOT picked here;
    they go through `_extract_pgs_ocr` separately (different cost +
    quality profile).

    Non-mkv containers (mp4, avi, ...) currently return None even if they
    contain a subtitle stream — mkvtoolnix only handles Matroska. Could
    add ffmpeg-based extraction later if needed; >95% of TV/movie rips
    are mkv anyway.
    """
    if video.suffix.lower() != ".mkv":
        return None

    found = _find_subtitle_track(video, lambda c: "subrip" in c or "srt" in c)
    if found is None:
        return None
    track_id, lang_label = found

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["mkvextract", "tracks", str(video), f"{track_id}:{dest}"],
            capture_output=True,
            timeout=MKVEXTRACT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[embedded] mkvextract failed for {video.name!r}: {e}", flush=True)
        return None
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        return None

    print(f"[embedded] extracted track {track_id} ({lang_label}) → {dest.name}", flush=True)
    return dest


def _extract_pgs_ocr(video: Path, dest: Path) -> Path | None:
    """Extract a PGS (bitmap) subtitle track from the mkv and OCR it to
    SRT via pgsrip → tesseract. Same-source extraction like
    `_extract_embedded`: timing is byte-perfect with the video master,
    so alass is skipped downstream. WER gate still applies — catches
    gross OCR failures (gibberish output) and forced-subs-only tracks
    (low cue count).

    OCR character accuracy on modern Blu-ray PGS rendering (clean sans-
    serif fonts) is ~95%. Italics and music / em-dash glyphs OCR worse.
    Acceptable as a fallback for releases that don't ship a SubRip
    track (e.g. Chernobyl and other HBO Blu-rays that only mux PGS).
    """
    if video.suffix.lower() != ".mkv":
        return None

    found = _find_subtitle_track(video, lambda c: "pgs" in c)
    if found is None:
        return None
    track_id, lang_label = found

    with tempfile.TemporaryDirectory(prefix="pgsocr-") as tmpdir:
        tmp = Path(tmpdir)
        sup_path = tmp / "track.sup"

        # 1. mkvextract PGS to a temp .sup file. Writing to tmpfs (or at
        # worst a temp dir) keeps the OCR-side scratch invisible to
        # Jellyfin / Infuse, which scan /artifact recursively.
        try:
            r = subprocess.run(
                ["mkvextract", "tracks", str(video), f"{track_id}:{sup_path}"],
                capture_output=True,
                timeout=MKVEXTRACT_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"[pgs-ocr] mkvextract failed for {video.name!r}: {e}", flush=True)
            return None
        if r.returncode != 0 or not sup_path.exists() or sup_path.stat().st_size == 0:
            return None

        # 2. pgsrip CLI consumes the .sup directly. It picks language
        # from the .sup metadata; if `und`, the output filename will
        # carry `.und.srt` instead of `.eng.srt` — we glob to find it.
        try:
            r = subprocess.run(
                ["pgsrip", str(sup_path)],
                capture_output=True,
                timeout=PGS_OCR_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"[pgs-ocr] pgsrip failed for {video.name!r}: {e}", flush=True)
            return None
        if r.returncode != 0:
            tail = (r.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()[-1:]
            print(f"[pgs-ocr] pgsrip rc={r.returncode} for {video.name!r}: {tail}", flush=True)
            return None

        produced = sorted(tmp.glob("track*.srt"))
        if not produced:
            print(f"[pgs-ocr] no .srt produced by pgsrip for {video.name!r}", flush=True)
            return None
        ocr_output = produced[0]
        if ocr_output.stat().st_size == 0:
            return None

        # 3. Promote to dest (under _sources/). Use shutil.copy2 — the
        # tempdir cleanup at scope exit will reap the OCR scratch.
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(ocr_output), str(dest))
        except OSError as exc:
            print(f"[pgs-ocr] copy to {dest} failed: {exc}", flush=True)
            return None

    print(f"[pgs-ocr] OCR'd PGS track {track_id} ({lang_label}) → {dest.name}", flush=True)
    return dest


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

    if tag == "embedded":
        return _extract_embedded(video, dest)

    if tag == "pgs-ocr":
        return _extract_pgs_ocr(video, dest)

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
      2. candidates   → /artifact/_sources/.../<stem>.{embedded,pgs-ocr,
                                                       bundled,
                                                       opensubtitles-*}.srt
      3. verify+sync  → /artifact/_sources/.../<stem>.verified.srt
                        (winner from step 2 → alass; embedded / pgs-ocr
                         wins skip alass since timing is already aligned
                         with the video master; or whisper itself if no
                         candidate passed the WER gate)
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

            if tag in ("embedded", "pgs-ocr"):
                # Both candidate kinds come out of the mkv container
                # itself — the subtitle stream was authored against the
                # same master as the video, so timing is byte-perfect.
                # Running alass here would only introduce drift on a
                # stream that was already correct, so we promote
                # directly.
                try:
                    verified_src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(cand_path), str(verified_src))
                    print(f"[pipeline] {video.name!r}: {tag} → verified.srt (alass skipped, same-source timing)", flush=True)
                    winner_tag = tag
                    break
                except OSError as exc:
                    print(f"[pipeline] {video.name!r}: {tag} → verified copy failed: {exc}", flush=True)
                    continue

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
