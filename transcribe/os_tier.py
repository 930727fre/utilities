"""OpenSubtitles k-try fetcher — download raw results one at a time,
run the caller-supplied accept callback on each, first passer wins.
Loser slots are kept on disk for replay reuse; k-try semantics
guarantee `max(-N.srt index) == winner`, so no un-indexed pointer file
is needed.
"""
import os
from pathlib import Path

from subs_finder import iter_candidates
from subs_verifier import verify_against_whisper

# With a paid OpenSubtitles subscription that lifts daily quota above the
# free-tier 20, set 3–5 to k-try the top-N raw OS results per tier
# (download, scrubbed-WER, first passer wins). Default 1 = single-slot
# k-try (behaves the same as the old top-1 path, minus the Haiku metadata
# gate that was removed). Quota-friendly — we only download the next
# candidate when the previous one failed WER.
OS_MAX_TRIES = max(1, int(os.environ.get("OS_MAX_TRIES", "1")))


def _indexed_pattern(dest: Path) -> str:
    """Build `<dest_no_ext>-{i}.srt` alongside `dest` for k-try indexed
    candidate paths."""
    base = str(dest)
    if base.endswith(".srt"):
        return base[:-4] + "-{i}.srt"
    return base + "-{i}.srt"


def os_ktry(video: Path, dest: Path, mode: str, accept) -> Path | None:
    """Lower-level k-try — hands `accept` the driver's seat. Used
    directly by the polluted-mode branch, which needs to substitute
    plot-check for WER as the accept signal.

    K-try semantics: download slot 1, run `accept`, first True wins.
    Loser slots stay on disk (below the winner's index) for replay
    reuse. Winner path returned directly; no un-indexed pointer.

    Returns None if no candidate passes, search returned zero, or
    quota is exhausted (all handled by `iter_candidates`)."""
    pattern = _indexed_pattern(dest)
    return iter_candidates(video, "en", pattern, mode, OS_MAX_TRIES, accept)


def fetch_os_ktry(
    video: Path,
    dest: Path,
    whisper_src: Path,
    windows: list[tuple[float, float]],
    mode: str,
) -> Path | None:
    """K-try OpenSubtitles fetch — download raw results one at a time, run
    WER after each, first passer wins. Returns the winning indexed
    `<dest_no_ext>-<i>.srt` path directly; loser slots (indices below the
    winner) also stay on disk for replay reuse.

    No un-indexed `dest` symlink is created — k-try semantics guarantee
    that the highest-index slot present on disk is the winner (loop exits
    on first pass), so "which one won" is derivable from `ls` alone. On
    the rare mid-run replay (pipeline died between winner-pick and
    verified.srt write), `iter_candidates` re-runs accept on cache-hit
    slots; for OS_MAX_TRIES=1 that's one WER call, negligible.

    Compared to a "download all N, pick lowest WER" strategy: strictly
    fewer downloads (stops as soon as one passes), same worst case, no
    ranking penalty (all passing candidates are above WER 0.5 anyway;
    "lowest passer" and "first passer" are both correct enough — alass
    handles the rest).

    Returns None if no candidate passes, if search returned zero, or if
    quota is exhausted (handled inside iter_candidates via its cache).
    """
    def _accept(cand: Path) -> bool:
        ok, reason = verify_against_whisper(whisper_src, cand, windows)
        print(f"[pipeline] {video.name!r}: {mode} k-try {cand.name} — "
              f"{'ACCEPT' if ok else 'REJECT'}: {reason}", flush=True)
        return ok

    return os_ktry(video, dest, mode, _accept)
