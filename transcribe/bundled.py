"""Bundled subtitle tier — search /bt/<wrapper>/ for .srt/.ass/.ssa
sidecars that belong to the video, verify content with either WER
(normal mode) or LLM smart-pick + plot-check (polluted mode).

Content-based selection; no filename heuristics. WER against clean
whisper is a strong enough signal to pick the right episode's srt
from a season pack; when whisper is >50% polluted, a Haiku smart-pick
narrows all candidates to one by filename convention and Opus plot-
check verifies content.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from bt_filter import BT_ROOT
from subs_verifier import (
    WER_PASS_MAX,
    cue_count_ok,
    smart_pick_bundled,
    wer_score,
)

_BUNDLED_SUB_EXTS = (".srt", ".ass", ".ssa")

# ASS/SSA conversion timeout — same shape as container_subs' embedded
# extraction (single-stream text conversion, no transcode).
FFMPEG_SUB_CONVERT_TIMEOUT = 60


def find_bt_wrapper(canonical_video: Path) -> Path | None:
    """Reverse-lookup the /bt/<wrapper>/ directory a canonical video was
    hardlinked from, by inode identity.

    Canonical /artifact/... and /bt/<wrapper>/.../<video> share an inode
    (bt_filter uses os.link, not copy), so a matching inode is a
    byte-level guarantee they're the same file. We walk /bt until we
    find that inode and return the top-level wrapper.

    Cost: O(files-in-/bt) per call, but only invoked once per bundled-
    tier lookup (result flows through `pick_bundled_min_wer` /
    `pick_bundled_smart_plot`, cached downstream via
    `_sources/<stem>.bundled.srt`). For a library on the order of
    thousands of bt files the walk is sub-second on SSD, which is
    negligible next to whisper's per-episode runtime.

    Rejected alternative: name-based sentinel lookup (parse
    `_processed/<wrapper>.filtered` content, use `sentinel.stem` as
    wrapper name). Was O(1)-per-sentinel but fragile — wrapper renames,
    sanitization mismatches, and LLM canonical drift all broke it and
    required inode fallback anyway. Simpler to skip the name path
    entirely."""
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


def _wrapper_side_video(canonical_video: Path, wrapper: Path) -> Path | None:
    """Find the video path inside `wrapper` that shares an inode with
    `canonical_video`. Used to reconstruct the release-group naming the
    bundled srts were paired against — smart-pick's filename heuristic
    gets more signal from `Show.S01E05.1080p.WEB-DL-GROUP.mkv` than
    from the canonical `Show (2020) - S01E05.mkv`."""
    try:
        target_inode = canonical_video.stat().st_ino
    except OSError:
        return None
    for entry in wrapper.rglob("*"):
        try:
            if entry.is_file() and entry.stat().st_ino == target_inode:
                return entry
        except OSError:
            continue
    return None


def _safe_rel(p: Path, base: Path) -> Path:
    try:
        return p.relative_to(base)
    except ValueError:
        return p


def _wrapper_subs(wrapper: Path) -> list[Path]:
    """Every .srt/.ass/.ssa under `wrapper`, sorted alphabetically. No
    filename filtering — content-based selection (WER in normal mode,
    LLM smart-pick + plot-check in polluted mode) picks the right one
    without trusting release-group naming conventions."""
    subs: list[Path] = []
    for ext in _BUNDLED_SUB_EXTS:
        subs.extend(wrapper.rglob(f"*{ext}"))
    return sorted(subs)


def _materialize(sub: Path, staged: Path, rel: Path, video: Path) -> Path | None:
    """Bring `sub` into `staged` as SubRip. SRT: copy. ASS/SSA: ffmpeg
    convert (`{\\an8}` / `\\N` override tags stripped on the way).
    Enforces the cue-count sanity check that drops forced-subs / partial
    tracks. Returns `staged` on success, None on any failure.

    Staging goes to a caller-owned tempdir — this function doesn't own
    the destination lifecycle; the caller decides whether to promote
    the winner elsewhere (e.g. copy to `_sources/<stem>.bundled.srt`)
    or discard."""
    if sub.suffix.lower() in (".ass", ".ssa"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", str(sub),
                 "-c:s", "srt",
                 str(staged)],
                capture_output=True,
                timeout=FFMPEG_SUB_CONVERT_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"[bundled] {video.name!r}: ASS convert failed for {rel}: {e}", flush=True)
            return None
        if r.returncode != 0 or not staged.exists() or staged.stat().st_size == 0:
            stderr = (r.stderr or b"").decode("utf-8", errors="replace").strip()
            print(f"[bundled] {video.name!r}: ASS convert empty for {rel} "
                  f"(rc={r.returncode}) {stderr[-100:]}", flush=True)
            return None
    else:
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(sub), str(staged))
        except OSError as exc:
            print(f"[bundled] {video.name!r}: stage failed for {rel}: {exc}", flush=True)
            return None

    if not cue_count_ok(staged, rel, video, tag="bundled"):
        return None
    return staged


def pick_bundled_min_wer(
    video: Path,
    dest: Path,
    whisper_src: Path,
    windows: list[tuple[float, float]],
) -> Path | None:
    """Normal mode: score every wrapper subtitle against whisper via
    `wer_score`, pick the lowest, gate at `WER_PASS_MAX`.

    Best-of-all (not first-passer). In a TV season pack with 20-30
    English srts, different episodes of the same show can have enough
    overlapping dialogue that a wrong-episode srt occasionally lands
    just under the 0.5 pass threshold before the correct one is
    iterated to — first-passer would pick that. Min-WER removes the
    ordering dependency: the correct episode's srt will always score
    lowest on its own audio.

    ASS/SSA candidates are ffmpeg-converted to SubRip in a scratch
    tempdir. Cue-count prefilter drops forced-subs / partial tracks
    before scoring runs. Winner is copied to `dest`. Returns `dest`
    on success, None when no wrapper is found, no candidates
    materialize, or every candidate scores above threshold.
    """
    wrapper = find_bt_wrapper(video)
    if wrapper is None:
        return None

    subs = _wrapper_subs(wrapper)
    if not subs:
        return None

    best: tuple[float, Path, str] | None = None  # (score, staged, rel)
    with tempfile.TemporaryDirectory(prefix="bundled-stage-") as tmpdir:
        tmp = Path(tmpdir)
        for i, sub in enumerate(subs, 1):
            rel = _safe_rel(sub, wrapper)
            staged = _materialize(sub, tmp / f"cand-{i}.srt", rel, video)
            if staged is None:
                continue
            score, reason = wer_score(whisper_src, staged, windows)
            if score is None:
                print(f"[bundled] {video.name!r}: SKIP {rel} — {reason}", flush=True)
                continue
            print(f"[bundled] {video.name!r}: score {rel} — {reason}", flush=True)
            if best is None or score < best[0]:
                best = (score, staged, str(rel))

        if best is None:
            return None
        wer, staged, rel = best
        if wer > WER_PASS_MAX:
            print(f"[bundled] {video.name!r}: best score {wer:.2f} > "
                  f"{WER_PASS_MAX} — REJECT (no candidate matched content)",
                  flush=True)
            return None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(staged), str(dest))
        except OSError as exc:
            print(f"[bundled] {video.name!r}: promote to dest failed: {exc}", flush=True)
            return None
        print(f"[bundled] {video.name!r}: WIN {rel} (WER {wer:.2f})", flush=True)
        return dest


def pick_bundled_smart_plot(
    video: Path,
    dest: Path,
    plot_accept,
) -> Path | None:
    """Polluted mode: Haiku smart-pick narrows all wrapper subs to one
    by filename convention, then `plot_accept` (bool) verifies its
    content via Opus plot-check. Bounds cost to one Haiku pick +
    one Opus plot-check per bundled attempt, regardless of how many
    srts the wrapper holds — iterating plot-check on a 30-sub season
    pack would run into dollars per episode.

    If only one candidate exists in the wrapper (after cue-count
    prefilter would apply), the smart-pick step is skipped and that
    candidate goes straight to `plot_accept`.

    Returns `dest` on success, None if no wrapper is found, LLM
    declined, or plot-check rejected the pick.
    """
    wrapper = find_bt_wrapper(video)
    if wrapper is None:
        return None

    subs = _wrapper_subs(wrapper)
    if not subs:
        return None

    if len(subs) > 1:
        # Prefer the wrapper-side video filename over the canonical
        # /artifact one — the release-group naming there is what the
        # bundled srts were paired against, so exact-stem / SxxExx
        # matches are more reliable. Falls back to canonical filename
        # if the wrapper-side hardlink can't be located.
        wrapper_video = _wrapper_side_video(video, wrapper) or video
        video_rel = _safe_rel(wrapper_video, wrapper)
        rel_subs = [_safe_rel(s, wrapper) for s in subs]
        picked_rel = smart_pick_bundled(wrapper.name, video_rel, rel_subs)
        if picked_rel is None:
            return None
        subs = [wrapper / picked_rel]

    with tempfile.TemporaryDirectory(prefix="bundled-stage-") as tmpdir:
        tmp = Path(tmpdir)
        for i, sub in enumerate(subs, 1):
            rel = _safe_rel(sub, wrapper)
            staged = _materialize(sub, tmp / f"cand-{i}.srt", rel, video)
            if staged is None:
                continue
            if not plot_accept(staged):
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(staged), str(dest))
            except OSError as exc:
                print(f"[bundled] {video.name!r}: promote to dest failed: {exc}", flush=True)
                return None
            print(f"[bundled] {video.name!r}: WIN {rel}", flush=True)
            return dest
    return None
