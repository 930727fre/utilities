"""One-shot migration: re-flow the flat /artifact/<wrapper>/ layout
(A-phase output) into the canonical Movies/TV layout produced by the
new (B-phase) bt_filter.

For each existing artifact wrapper:

  1. Look up the matching /bt/<wrapper>/ (same name)
  2. Re-run bt_filter on the bt-side wrapper — that creates canonical
     paths under /artifact/Movies/ or /artifact/TV/ with:
       - hardlinks to the bt-side videos (same inode as before, zero
         extra disk)
       - FRESH (un-annotated) SRT copies from bundled bt-side .srt
     and writes /artifact/_processed/<wrapper>.filtered with the new
     paths as a manifest
  3. For each canonical video, find the matching old /artifact/<wrapper>/
     file by inode (videos are hardlinks → same inode → easy match) and
     MOVE its sidecars over the bt_filter fresh copies:
       <stem>.srt           overwrites the bundled SRT (preserves ※)
       <stem>.zh-tw.srt     becomes <canonical_stem>.zh-tw.srt
       <stem>.zh-tw.srt.error  preserved likewise
  4. rmtree the old /artifact/<wrapper>/ (its .filtered sentinel is
     A-era and unused now; its video hardlinks go away as just one of
     two names — the bt-side copy still exists, and the canonical
     hardlink is now the artifact entry point)

The script is idempotent — re-running after success skips wrappers
that already have a sentinel and an empty old-wrapper dir.
"""
import os
import shutil
import sys
from pathlib import Path

import bt_filter

ARTIFACT_ROOT = bt_filter.ARTIFACT_ROOT
BT_ROOT = bt_filter.BT_ROOT
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}

# Canonical roots under /artifact/ — anything else at top level (that
# isn't _processed) is an A-era flat wrapper waiting to be migrated.
CANONICAL_TOP = {"Movies", "TV", "_processed"}


def _flat_artifact_wrappers() -> list[Path]:
    if not ARTIFACT_ROOT.exists():
        return []
    return sorted(
        p for p in ARTIFACT_ROOT.iterdir()
        if p.is_dir() and p.name not in CANONICAL_TOP
    )


def _find_old_video_by_inode(old_wrapper: Path, inode: int) -> Path | None:
    """Match a canonical video back to its old flat-artifact counterpart
    via inode (both are hardlinks to the same bt-side file)."""
    try:
        entries = list(old_wrapper.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in VIDEO_EXTS:
            continue
        try:
            if entry.stat().st_ino == inode:
                return entry
        except OSError:
            continue
    return None


def _move_sidecars(old_video: Path, canonical_video: Path) -> dict[str, int]:
    """Move <stem>.srt / <stem>.zh-tw.srt / <stem>.zh-tw.srt.error from
    old_video's parent to canonical_video's parent, renamed to the
    canonical stem so the annotation pipeline finds them next time.

    The bt_filter just-completed pass left a fresh (un-annotated) bundled
    SRT in the canonical .srt slot — moving the old annotated SRT over
    it preserves the ※ markers the user has paid for."""
    moved = {"srt": 0, "zh": 0, "zh_err": 0}

    old_dir = old_video.parent
    old_stem = old_video.stem
    canonical_dir = canonical_video.parent
    canonical_stem = canonical_video.stem

    pairs = [
        (old_dir / f"{old_stem}.srt",            canonical_dir / f"{canonical_stem}.srt",            "srt"),
        (old_dir / f"{old_stem}.zh-tw.srt",      canonical_dir / f"{canonical_stem}.zh-tw.srt",      "zh"),
        (old_dir / f"{old_stem}.zh-tw.srt.error", canonical_dir / f"{canonical_stem}.zh-tw.srt.error", "zh_err"),
    ]
    for src, dst, key in pairs:
        if not src.is_file():
            continue
        try:
            canonical_dir.mkdir(parents=True, exist_ok=True)
            # shutil.move will overwrite the target on POSIX — exactly
            # what we want for .srt (replacing the fresh bundled copy
            # with the old annotated one).
            shutil.move(str(src), str(dst))
            moved[key] += 1
        except OSError as e:
            print(f"  ! failed to move {src.name}: {e}", flush=True)
    return moved


def migrate_one(old_wrapper: Path) -> dict:
    """Returns a small stats dict; '+' fields are counts."""
    stats = {"matched": 0, "videos_no_match": 0, "srt_moved": 0,
             "zh_moved": 0, "zh_err_moved": 0}

    wrapper_name = old_wrapper.name
    bt_wrapper = BT_ROOT / wrapper_name
    if not bt_wrapper.is_dir():
        print(f"SKIP {wrapper_name}: no bt-side wrapper", flush=True)
        return stats

    # Force bt_filter to re-process even if a B-era sentinel happens to
    # exist already (e.g. from an interrupted earlier migration run).
    sentinel = bt_filter._sentinel_for(wrapper_name)
    if sentinel.exists():
        try:
            sentinel.unlink()
        except OSError:
            pass

    bt_filter.filter_wrapper(bt_wrapper)
    canonical_videos = bt_filter.load_manifest(wrapper_name)

    if not canonical_videos:
        print(f"  warning: bt_filter produced no canonical videos for {wrapper_name}", flush=True)
        # Still drop the old wrapper dir — its hardlinks are redundant
        # now and the .filtered sentinel is unused.
        shutil.rmtree(old_wrapper, ignore_errors=True)
        return stats

    for canonical_video in canonical_videos:
        try:
            inode = canonical_video.stat().st_ino
        except OSError:
            continue
        old_video = _find_old_video_by_inode(old_wrapper, inode)
        if old_video is None:
            stats["videos_no_match"] += 1
            continue
        stats["matched"] += 1
        moved = _move_sidecars(old_video, canonical_video)
        stats["srt_moved"] += moved["srt"]
        stats["zh_moved"] += moved["zh"]
        stats["zh_err_moved"] += moved["zh_err"]

    # Old wrapper dir cleanup. Its remaining files (video hardlinks,
    # .filtered sentinel, possibly unmatched sidecars) are all redundant
    # at this point — the canonical layout has the authoritative copies.
    try:
        shutil.rmtree(old_wrapper)
    except OSError as e:
        print(f"  ! failed to remove old wrapper {old_wrapper.name}: {e}", flush=True)
    return stats


def main() -> int:
    flats = _flat_artifact_wrappers()
    if not flats:
        print("no flat artifact wrappers to migrate", flush=True)
        return 0
    print(f"migrating {len(flats)} flat artifact wrapper(s) into canonical layout", flush=True)

    total = {"matched": 0, "videos_no_match": 0, "srt_moved": 0,
             "zh_moved": 0, "zh_err_moved": 0}

    for wrapper in flats:
        short = wrapper.name[:60]
        stats = migrate_one(wrapper)
        print(
            f"  {short:<60}  matched={stats['matched']:3d}  "
            f"srt={stats['srt_moved']:3d}  zh={stats['zh_moved']:3d}  "
            f"zh_err={stats['zh_err_moved']}  no_match={stats['videos_no_match']}",
            flush=True,
        )
        for k, v in stats.items():
            total[k] += v

    print(
        f"\ndone: matched={total['matched']}  srt_moved={total['srt_moved']}  "
        f"zh_moved={total['zh_moved']}  zh_err_moved={total['zh_err_moved']}  "
        f"unmatched_videos={total['videos_no_match']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
