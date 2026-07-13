"""In-container subtitle extraction — embedded text tracks (subrip /
mov_text / webvtt / ass) and PGS bitmap tracks (OCR via pgsrip +
tesseract). Both are same-source with the video (same release group
authored the container), so timing is byte-perfect vs the video master
and the caller downstream skips alass.

Container-agnostic — mkv, mp4, WebM, mov, ts. ffprobe enumerates
subtitle streams; ffmpeg extracts (`-c:s srt` transcodes any text codec
to SubRip on the way out).
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

FFPROBE_PROBE_TIMEOUT = 30         # JSON stream probe — milliseconds in practice
FFMPEG_SUB_EXTRACT_TIMEOUT = 60    # demux one subtitle stream (no transcode) — seconds on a movie-sized container
PGS_OCR_TIMEOUT = 10 * 60          # tesseract OCR of a 50-min episode's PGS — ~1-3 min typical

# Text-based subtitle codecs ffmpeg can convert to SubRip via `-c:s srt`.
# ASS/SSA included: ffmpeg strips `{\an8}` / `\N` override tags on the way
# out. Heavy typesetting (anime karaoke, sign translations) can leave
# residue — cue-count prefilter catches the pathological cases and the
# pipeline falls through to the next candidate.
_TEXT_SUB_CODECS = {"subrip", "mov_text", "webvtt", "ass", "ssa"}


def _find_subtitle_track(video: Path, codec_match) -> tuple[int, str] | None:
    """Probe `video` for the best subtitle stream of a given codec family.
    Returns `(stream_index, lang_label)` where `stream_index` is the
    global stream index usable with `ffmpeg -map 0:<idx>`, and
    `lang_label` is "eng" / "und" depending on which fallback tier
    matched. None if no stream matches the predicate.

    Container-agnostic via ffprobe — mkv (SubRip / PGS), mp4 (mov_text),
    WebM (WebVTT), mov, ts, etc.

    `codec_match(codec_lowered: str) -> bool` decides which ffprobe
    codec_name strings count. Examples:
      lambda c: c in ("subrip", "mov_text", "webvtt", "ass")  # text subs
      lambda c: c == "hdmv_pgs_subtitle"                       # PGS only

    Tracks with `disposition.forced` set are skipped — those only
    translate non-dialogue visual elements (foreign signs, paper
    notes, on-screen text) and are useless against a whisper
    transcript that covers the full spoken dialogue. WER would catch
    a forced-track winner, but skipping at the probe layer avoids
    wasted extraction / OCR work and lets a sibling full track in
    the same container be picked instead. Concrete failure mode this
    handles: HBO Chernobyl muxes a forced English PGS track BEFORE
    the full English PGS one; without this skip we'd OCR the forced
    one and get 12 cues per episode.

    Two-pass selection: explicit eng/en tag wins; otherwise the first
    track tagged und/zxx/empty is taken as a content fallback (BluRay
    rips frequently ship English subs without language metadata). The
    cue-count / WER gate downstream verifies the content is usable.
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "s",
             "-show_streams",
             "-print_format", "json",
             str(video)],
            capture_output=True,
            timeout=FFPROBE_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[ffprobe] probe failed for {video.name!r}: {e}", flush=True)
        return None
    if probe.returncode != 0:
        return None
    try:
        info = json.loads(probe.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    english_id: int | None = None
    fallback_id: int | None = None
    for stream in info.get("streams") or []:
        # `-select_streams s` already filtered to subtitle streams,
        # but defend against malformed probe output anyway.
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").lower()
        if not codec_match(codec):
            continue
        disposition = stream.get("disposition") or {}
        if disposition.get("forced"):
            continue
        tags = stream.get("tags") or {}
        lang = str(tags.get("language") or "").lower()
        idx = stream.get("index")
        if idx is None:
            continue
        if lang in ("eng", "en") and english_id is None:
            english_id = idx
        elif lang in ("", "und", "zxx") and fallback_id is None:
            fallback_id = idx

    if english_id is not None:
        return english_id, "eng"
    if fallback_id is not None:
        return fallback_id, "und"
    return None


def extract_embedded(video: Path, dest: Path) -> Path | None:
    """Extract the first usable English text-subtitle track from `video`
    to `dest` as SubRip. Returns dest on success, None if no extractable
    track exists.

    Container-agnostic — works on mkv (subrip), mp4 (mov_text), WebM
    (webvtt), etc. ffmpeg's `-c:s srt` transcodes any text-based
    subtitle codec into SubRip format on the way out.

    Same-source extraction: the subtitle stream was authored against
    the same master as the video, so timing is exact — pipeline skips
    alass for this candidate. PGS / VobSub tracks are NOT picked here;
    they go through `extract_pgs_ocr` separately (different cost +
    quality profile).
    """
    found = _find_subtitle_track(video, lambda c: c in _TEXT_SUB_CODECS)
    if found is None:
        return None
    stream_idx, lang_label = found

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(video),
             "-map", f"0:{stream_idx}",
             "-c:s", "srt",
             str(dest)],
            capture_output=True,
            timeout=FFMPEG_SUB_EXTRACT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[embedded] ffmpeg extract failed for {video.name!r}: {e}", flush=True)
        return None
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        if stderr:
            print(f"[embedded] ffmpeg rc={r.returncode} for {video.name!r}: {stderr[-200:]}", flush=True)
        return None

    print(f"[embedded] extracted stream {stream_idx} ({lang_label}) → {dest.name}", flush=True)
    return dest


def extract_pgs_ocr(video: Path, dest: Path) -> Path | None:
    """Extract a PGS (bitmap) subtitle track and OCR it to SRT via
    pgsrip → tesseract. Same-source extraction like `extract_embedded`:
    timing is byte-perfect with the video master, so alass is skipped
    downstream. Cue-count gate still applies downstream — catches
    complete OCR failures and forced-subs tracks that slipped past the
    probe filter.

    Container-agnostic — primarily targets mkv (the canonical PGS
    carrier from Blu-ray rips), but ffmpeg also handles mp4/mov when a
    `hdmv_pgs_subtitle` stream is present. PGS in mp4 is rare but real
    (some HEVC mp4 remuxes preserve the original PGS tracks).

    OCR character accuracy on modern Blu-ray PGS rendering (clean
    sans-serif fonts) is ~95%. Italics and music / em-dash glyphs OCR
    worse. Acceptable as a fallback for releases that don't ship a
    text subtitle track (e.g. Chernobyl and other HBO Blu-rays that
    only mux PGS).
    """
    found = _find_subtitle_track(video, lambda c: c == "hdmv_pgs_subtitle")
    if found is None:
        return None
    stream_idx, lang_label = found

    with tempfile.TemporaryDirectory(prefix="pgsocr-") as tmpdir:
        tmp = Path(tmpdir)
        sup_path = tmp / "track.sup"

        # 1. ffmpeg demux PGS bitstream to a temp .sup file. `-c:s copy`
        # keeps the raw PGS packets intact (no transcode); `-f sup`
        # picks the raw-PGS muxer explicitly. Writing to tmpfs / temp
        # keeps the OCR-side scratch invisible to Jellyfin / Infuse,
        # which scan /artifact recursively.
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", str(video),
                 "-map", f"0:{stream_idx}",
                 "-c:s", "copy",
                 "-f", "sup",
                 str(sup_path)],
                capture_output=True,
                timeout=FFMPEG_SUB_EXTRACT_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"[pgs-ocr] ffmpeg PGS demux failed for {video.name!r}: {e}", flush=True)
            return None
        if r.returncode != 0 or not sup_path.exists() or sup_path.stat().st_size == 0:
            stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
            if stderr:
                print(f"[pgs-ocr] ffmpeg rc={r.returncode} for {video.name!r}: {stderr[-200:]}", flush=True)
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

    print(f"[pgs-ocr] OCR'd PGS stream {stream_idx} ({lang_label}) → {dest.name}", flush=True)
    return dest
