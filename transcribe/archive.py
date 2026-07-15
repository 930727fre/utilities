"""Auto-archive canonical SRTs + auto-attach on re-download.

Every canonical SRT the pipeline produces gets mirrored to
`data/archive/<title>/…` under a flat show-title tree (Movies/ and TV/
prefixes stripped — the tree groups by title, not media kind).

Attach happens at ONE point: `process_bt_file` Stage 0. Every video
goes through the same pipeline entry, whether it's a first-time
download, a re-download of previously-archived content, or a deep-↻
retry. If archive has the SRT (matched by exact stem + layout), Stage 0
copies it to canonical; downstream stages see the file exists and skip
cleanly. No LLM cost, no GPU cost, all-archive wrappers finish within
one reconciler tick of arrival.

Layout is symmetric: `mirror_to_archive` writes to a path derived from
the canonical relative path (`Movies/<title>/…` → `<title>/…`), and
`attach_from_archive` reads from exactly the same computed path. No
fuzzy matching, no rglob search — one file = one location.

The tradeoff: cross-release doesn't auto-match. If archive was written
from `Blade Runner (1982) Director's Cut.mkv` and the user later
downloads `Blade Runner (1982) Final Cut.mkv`, the archive lookup
misses because the stems differ. Rare enough to accept; user can
manually `cp` if they care.

Manual SRT salvage: drop directly at the canonical location, NOT into
archive. Reconciler sees `has_srt=True` and dispatches only the missing
downstream stages (translate). Archive is populated automatically on
each canonical write, so future re-downloads of the same title
auto-attach.

Deletion: `delete_torrent` doesn't touch archive (SRTs survive
disk-freeing for future re-attach). Deep-↻ retry DOES delete the
archive entry for the affected video (otherwise the same wrong SRT
would just come back on the next pipeline run).

Toggle: `SRT_ARCHIVE_ENABLED=false` disables both mirror + lookup.
Default enabled.
"""
import os
import shutil
from pathlib import Path

from bt_filter import ARTIFACT_ROOT

ARCHIVE_ROOT = Path(os.environ.get("SRT_ARCHIVE_ROOT", "/app/data/archive"))
ENABLED = os.environ.get("SRT_ARCHIVE_ENABLED", "true").lower() != "false"

_ZH_SUFFIX = ".zh-tw.srt"


def _archive_dir_for(canonical_video: Path) -> Path | None:
    """Directory under ARCHIVE_ROOT that mirrors `canonical_video`'s
    parent folder. None if the video isn't under a recognized Movies/TV
    layout below ARTIFACT_ROOT.

    Movies example:
      canonical_video = /artifact/Movies/Toy Story 5 (2026)/Toy Story 5 (2026).mkv
      returns          /archive/Toy Story 5 (2026)

    TV example:
      canonical_video = /artifact/TV/Silo (2023)/Season 01/Silo (2023) - S01E01.mkv
      returns          /archive/Silo (2023)/Season 01
    """
    try:
        rel = canonical_video.resolve().relative_to(ARTIFACT_ROOT.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2 or parts[0] not in ("Movies", "TV"):
        return None
    return ARCHIVE_ROOT.joinpath(*parts[1:-1])


def archive_paths_for(canonical_video: Path) -> tuple[Path, Path] | None:
    """Return `(english_srt_path, zh_srt_path)` under archive for the
    given canonical video, or None if the video isn't in a recognized
    Movies/TV layout. Paths may not exist — this is a naming helper.
    Used by the pipeline's deep-retry to know which archive entries to
    nuke alongside canonical + `_sources/`."""
    d = _archive_dir_for(canonical_video)
    if d is None:
        return None
    return (d / f"{canonical_video.stem}.srt",
            d / f"{canonical_video.stem}{_ZH_SUFFIX}")


# ── mirror on canonical write ────────────────────────────────────────────

def mirror_to_archive(canonical_srt: Path) -> None:
    """Called after each canonical SRT atomic write (English or zh-tw).
    Copies to `data/archive/<title>/<relative path>` — Movies/TV prefix
    stripped so archive tree is media-kind-agnostic. Overwrites if a
    prior copy exists (rare, desired: latest pipeline output wins).

    Silent no-op if disabled, if the source path isn't under ARTIFACT_ROOT
    (YouTube dumps, staging paths), or if the shape isn't Movies|TV/.../
    Failures log a warning; mirror can never break the main pipeline."""
    if not ENABLED:
        return
    try:
        rel = canonical_srt.resolve().relative_to(ARTIFACT_ROOT.resolve())
    except ValueError:
        return
    parts = rel.parts
    if len(parts) < 2 or parts[0] not in ("Movies", "TV"):
        return
    dest = ARCHIVE_ROOT.joinpath(*parts[1:])
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical_srt, dest)
    except OSError as exc:
        print(f"[archive] mirror failed for {rel}: {exc}", flush=True)


# ── attach ──────────────────────────────────────────────────────────────

def attach_from_archive(canonical_video: Path) -> None:
    """Copy `<stem>.srt` and `<stem>.zh-tw.srt` from archive into the
    canonical location, for whichever ones exist in archive and are
    missing at canonical.

    Silent no-op if archive is disabled or nothing applies. Per-file
    OSErrors log and continue — never raise, so pipeline callers can
    safely fall through to whisper on any attach failure."""
    if not ENABLED:
        return
    paths = archive_paths_for(canonical_video)
    if paths is None:
        return
    archive_eng, archive_zh = paths

    canonical_srt = canonical_video.with_suffix(".srt")
    canonical_zh = canonical_video.parent / f"{canonical_video.stem}{_ZH_SUFFIX}"

    if archive_eng.is_file() and not canonical_srt.exists():
        try:
            canonical_srt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive_eng, canonical_srt)
            print(f"[archive] attached → {canonical_srt.name}", flush=True)
        except OSError as exc:
            print(f"[archive] attach failed for {canonical_video.name}: {exc}", flush=True)

    if archive_zh.is_file() and not canonical_zh.exists():
        try:
            shutil.copy2(archive_zh, canonical_zh)
            print(f"[archive] attached zh → {canonical_zh.name}", flush=True)
        except OSError as exc:
            print(f"[archive] zh attach failed for {canonical_video.name}: {exc}", flush=True)
