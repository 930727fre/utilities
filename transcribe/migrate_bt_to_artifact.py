"""One-shot migration: hardlink existing /bt/<wrapper>/ artifact-shape
files into /artifact/<wrapper>/, plus drop the dead _bt_originals/
backup left behind by the 2026-06-27 live-hls reverse-migration.

Run once inside the transcribe-app container after rebuild:

    docker compose exec transcribe-app python migrate_bt_to_artifact.py

The script is idempotent — re-running it is safe (already-migrated
wrappers / files are detected and skipped). /bt/ is read-only here
(reads + os.link), nothing is moved or deleted from the bt side, so
aria2's seeding files stay intact.

Pre-condition: every existing /bt/<wrapper>/ is "artifact shape" from
previous bt_filter runs — flat at root, with .filtered sentinel.
Wrappers without .filtered are assumed to be fresh / partial and are
skipped (the new bt_filter will process them on its next scan tick).
"""
import os
import shutil
import sys
from pathlib import Path

BT_ROOT = Path("/app/data/bt")
ARTIFACT_ROOT = Path("/app/data/artifact")
DEAD_BT_ORIGINALS = Path("/app/data/_bt_originals")

# Stay file-extension-conservative: hardlink everything found at the
# wrapper root (videos, .srt, .zh-tw.srt, .filtered, .zh-tw.srt.error)
# except aria2's .torrent resume metadata. That stays on the bt side
# only so an aria2 re-spawn (resume_all) finds it where it expects.
SKIP_SUFFIXES = {".torrent"}


_warned_about_copy_fallback = False


def hardlink_or_copy(src: Path, dst: Path) -> str:
    """Try os.link(); fall back to shutil.copy2 on EXDEV / cross-fs.
    Returns one of: 'linked', 'copied', 'skipped' (target existed),
    or 'failed: <reason>'.

    Prints a one-time warning to stderr the first time a hardlink fails,
    surfacing the underlying errno so the user knows the byte-copy
    fallback kicked in (with all that implies for disk space + time)."""
    global _warned_about_copy_fallback
    if dst.exists():
        return "skipped"
    try:
        os.link(str(src), str(dst))
        return "linked"
    except OSError as e:
        if not _warned_about_copy_fallback:
            print(
                f"\n!!! os.link failed ({e}); falling back to byte copy. "
                f"Check bind mount setup — bt and artifact should share a mount.\n",
                file=sys.stderr,
                flush=True,
            )
            _warned_about_copy_fallback = True
        try:
            shutil.copy2(str(src), str(dst))
            return "copied"
        except OSError as e2:
            return f"failed: link={e.strerror!r} copy={e2.strerror!r}"


def migrate_wrapper(wrapper: Path) -> dict:
    """Migrate one /bt/<wrapper>/ into /artifact/<wrapper>/. Returns a
    small stats dict for the per-wrapper summary line."""
    stats = {"linked": 0, "copied": 0, "skipped": 0, "failed": 0}
    target_wrapper = ARTIFACT_ROOT / wrapper.name
    target_wrapper.mkdir(parents=True, exist_ok=True)
    for entry in wrapper.iterdir():
        if not entry.is_file():
            # Skip nested directories (Sample/, Subs/) — we don't want
            # them in /artifact/; the LLM was supposed to flatten earlier.
            continue
        if entry.suffix.lower() in SKIP_SUFFIXES:
            continue
        outcome = hardlink_or_copy(entry, target_wrapper / entry.name)
        if outcome == "linked":
            stats["linked"] += 1
        elif outcome.startswith("copied"):
            stats["copied"] += 1
        elif outcome == "skipped":
            stats["skipped"] += 1
        else:
            stats["failed"] += 1
            print(f"  ✗ {entry.name}: {outcome}")
    return stats


def main() -> int:
    if not BT_ROOT.exists():
        print(f"error: {BT_ROOT} does not exist — wrong container? wrong mount?", flush=True)
        return 1
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

    wrappers = sorted(p for p in BT_ROOT.iterdir() if p.is_dir())
    print(f"found {len(wrappers)} wrapper(s) under {BT_ROOT}", flush=True)

    migrated = 0
    skipped_unfiltered = 0
    for wrapper in wrappers:
        if not (wrapper / ".filtered").exists():
            print(f"SKIP (no .filtered): {wrapper.name}", flush=True)
            skipped_unfiltered += 1
            continue
        short = wrapper.name[:60]
        stats = migrate_wrapper(wrapper)
        print(
            f"  {short:<60}  linked={stats['linked']:3d}  copied={stats['copied']:2d}  "
            f"skipped={stats['skipped']:3d}  failed={stats['failed']}",
            flush=True,
        )
        migrated += 1

    print(f"\ndone: migrated {migrated} wrapper(s), skipped {skipped_unfiltered} unfiltered", flush=True)

    # Drop the dead _bt_originals/ if it exists. Small (~580K when this
    # script was written) leftover from the live-hls reverse-migration
    # on 2026-06-27 that nothing reads any more.
    if DEAD_BT_ORIGINALS.is_dir():
        size_before = sum(
            f.stat().st_size for f in DEAD_BT_ORIGINALS.rglob("*") if f.is_file()
        )
        shutil.rmtree(DEAD_BT_ORIGINALS, ignore_errors=True)
        print(f"removed dead {DEAD_BT_ORIGINALS} ({size_before / 1024:.0f}K)", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
