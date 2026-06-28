"""Replace video files in /app/data/artifact/ that are byte-copies (left
over from the EXDEV bug in the first migration run, before the docker
mount was collapsed to a single bind) with proper hardlinks pointing at
the matching /app/data/bt/ source.

Detection is by st_nlink: a freshly-hardlinked file has nlink >= 2 (one
name on each side); a stand-alone copy has nlink == 1. We only touch
videos — SRT files in /artifact/ deliberately diverge from /bt/ once
※ annotation runs over them, so flipping those to hardlinks would
silently propagate annotation edits into the bt-seeded copy.

Idempotent — re-running after a successful pass is a no-op (everything
shows nlink>=2 and gets reported as already-linked).
"""
import os
import sys
from pathlib import Path

ARTIFACT_ROOT = Path("/app/data/artifact")
BT_ROOT = Path("/app/data/bt")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}


def _find_bt_source(bt_wrapper: Path, filename: str) -> Path | None:
    """The bt-side file may live at the wrapper root or inside a nested
    folder (Season 01/, Subs siblings, etc.). Match by filename and pick
    the first hit. Returns None if no match."""
    if not bt_wrapper.is_dir():
        return None
    for candidate in bt_wrapper.rglob(filename):
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    if not ARTIFACT_ROOT.exists():
        print(f"error: {ARTIFACT_ROOT} does not exist", flush=True)
        return 1

    fixed = 0
    already_linked = 0
    bt_missing = 0
    size_mismatch = 0
    freed_bytes = 0

    for wrapper in sorted(p for p in ARTIFACT_ROOT.iterdir() if p.is_dir()):
        bt_wrapper = BT_ROOT / wrapper.name
        for file in wrapper.iterdir():
            if not file.is_file():
                continue
            if file.suffix.lower() not in VIDEO_EXTS:
                continue

            try:
                st = file.stat()
            except OSError as e:
                print(f"stat failed for {file}: {e}", flush=True)
                continue

            if st.st_nlink >= 2:
                already_linked += 1
                continue

            bt_file = _find_bt_source(bt_wrapper, file.name)
            if bt_file is None:
                print(f"NO BT SOURCE: {wrapper.name}/{file.name}", flush=True)
                bt_missing += 1
                continue

            try:
                bt_st = bt_file.stat()
            except OSError as e:
                print(f"bt stat failed for {bt_file}: {e}", flush=True)
                continue

            if bt_st.st_size != st.st_size:
                print(
                    f"SIZE MISMATCH (refusing): {wrapper.name}/{file.name} "
                    f"artifact={st.st_size} bt={bt_st.st_size}",
                    flush=True,
                )
                size_mismatch += 1
                continue

            # Safe replacement: link the bt source under a temp name in
            # the same directory, atomic-rename over the copy. Removes the
            # window where the file briefly doesn't exist.
            tmp = file.with_suffix(file.suffix + ".relink.tmp")
            try:
                if tmp.exists():
                    tmp.unlink()
                os.link(str(bt_file), str(tmp))
                os.replace(str(tmp), str(file))
            except OSError as e:
                print(f"relink failed for {file}: {e}", flush=True)
                continue

            print(
                f"FIXED: {wrapper.name}/{file.name}  ({st.st_size / 1_000_000_000:.2f}G freed)",
                flush=True,
            )
            fixed += 1
            freed_bytes += st.st_size

    print(
        f"\nfixed={fixed}  already_linked={already_linked}  "
        f"bt_missing={bt_missing}  size_mismatch={size_mismatch}  "
        f"freed={freed_bytes / 1_000_000_000:.1f}G",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
