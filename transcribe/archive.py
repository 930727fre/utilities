"""Auto-archive canonical SRTs + auto-attach on re-download.

Every canonical SRT the pipeline produces gets mirrored to
`data/archive/<title>/…` under a flat show-title tree (Movies/ and TV/
prefixes stripped — the tree groups by title, not media kind).

Attach happens at `bt_filter.filter_wrapper` time: right after the
wrapper's videos are hardlinked to `/artifact/…/canonical.mkv`,
`attach_wrapper_from_archive` looks for `/archive/<canonical_show>/`
by DIRECT STRING MATCH against the archive folder name. If it exists,
the matching English SRT (+ zh-tw sibling) is copied next to the
canonical video. Downstream `_scan_bt` sees `has_srt=True` and skips
the whole whisper + annotate pipeline for that video — the archive
tier's whole point.

Direct string match works because:
1. Archive folders are created by `mirror_to_archive`, which uses the
   canonical path shape (`Movies/<title>/…` → `archive/<title>/…`)
2. bt_filter's Opus prompt pins canonical titles to TMDb exactly
3. Same LLM policy + temperature=0 → same canonical title across runs
   → same folder name → deterministic match

No LLM call is required for attach. The prior Gemini Flash Lite
fuzzy-match implementation was replaced after it mis-matched
"Toy Story 5 (2026)" → "Backroom (2026)" on year alone.

Archive is a superset of everything the pipeline has ever produced.
Deleting artifact/{Movies,TV}/<title>/ (e.g. via `delete_torrent`) does
NOT touch archive/<title>/ — that's the whole point: SRTs survive
disk-freeing wrapper deletes and re-attach for free next time.

Toggle: `SRT_ARCHIVE_ENABLED=false` disables both mirror + lookup.
Default enabled.
"""
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from bt_filter import ARTIFACT_ROOT

ARCHIVE_ROOT = Path(os.environ.get("SRT_ARCHIVE_ROOT", "/app/data/archive"))
ENABLED = os.environ.get("SRT_ARCHIVE_ENABLED", "true").lower() != "false"

_EP_RE = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")
_ZH_SUFFIX = ".zh-tw.srt"


# ── episode key ──────────────────────────────────────────────────────────

def _episode_key(stem: str) -> Optional[tuple[int, int]]:
    """Extract (season, episode) tuple from a filename stem, or None if the
    stem isn't a TV episode. Deterministic, no LLM needed."""
    m = _EP_RE.search(stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


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


# ── attach on new BT arrival ────────────────────────────────────────────

def _find_archive_english(canonical_video: Path, archive_folder: Path) -> Optional[Path]:
    """Inside a matched archive folder, locate the English SRT that
    corresponds to `canonical_video`. Returns None if nothing matches.

    Movies: folder should hold exactly one non-zh-tw SRT (single-SRT
      convention). Ambiguity (multiple English SRTs from a re-download
      edge case) → decline rather than guess.
    TV: match by SxxExx key extracted from filename stems."""
    try:
        parts = canonical_video.resolve().relative_to(ARTIFACT_ROOT.resolve()).parts
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    if parts[0] == "Movies":
        eng = [
            p for p in archive_folder.rglob("*.srt")
            if not p.name.endswith(_ZH_SUFFIX)
        ]
        return eng[0] if len(eng) == 1 else None
    # TV
    ep = _episode_key(canonical_video.stem)
    if ep is None:
        return None
    for p in archive_folder.rglob("*.srt"):
        if p.name.endswith(_ZH_SUFFIX):
            continue
        if _episode_key(p.stem) == ep:
            return p
    return None


def _find_archive_zh_sibling(archive_english_srt: Path) -> Optional[Path]:
    """Given the matched English archive SRT, return the sibling
    <stem>.zh-tw.srt if the archive also has a Chinese translation."""
    zh = archive_english_srt.parent / f"{archive_english_srt.stem}{_ZH_SUFFIX}"
    return zh if zh.exists() else None


def attach_wrapper_from_archive(canonical_videos: list[Path]) -> None:
    """For each canonical video the wrapper just produced, check if
    `/archive/<canonical_show>/` exists (direct string match on the
    show folder name) and holds a matching SRT. If yes, copy it to
    the canonical location + zh-tw sibling.

    Runs once per wrapper, called by `bt_filter.filter_wrapper` after
    it hardlinks videos to canonical. The subsequent BT-work scan tick
    sees `has_srt=True` for these videos and skips whisper + annotate
    entirely.

    Silent no-op if archive is disabled or the folder doesn't exist —
    a miss just means the video goes through the full pipeline like
    a fresh download.

    Failures per-file are logged and don't halt the wrapper — a partial
    attach is fine (fully-missed videos just fall through to pipeline)."""
    if not ENABLED or not ARCHIVE_ROOT.is_dir():
        return
    for canonical_video in canonical_videos:
        try:
            rel = canonical_video.resolve().relative_to(ARTIFACT_ROOT.resolve())
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2 or parts[0] not in ("Movies", "TV"):
            continue
        show_title = parts[1]
        archive_folder = ARCHIVE_ROOT / show_title
        if not archive_folder.is_dir():
            continue
        eng = _find_archive_english(canonical_video, archive_folder)
        if eng is None:
            continue
        canonical_srt = canonical_video.with_suffix(".srt")
        try:
            canonical_srt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(eng, canonical_srt)
            print(f"[archive] attached {show_title!r} → {canonical_video.name}", flush=True)
        except OSError as exc:
            print(f"[archive] attach failed for {canonical_video.name}: {exc}", flush=True)
            continue
        # Fire wrapper-aggregating notification. The sentinel is already
        # written by bt_filter (step 3, before this attach step 4), so
        # notify_success can look up the wrapper by canonical path. The
        # last attach in an all-archive wrapper naturally observes
        # all-terminal state and fires the wrapper summary.
        try:
            from notifier import notify_success
            notify_success(canonical_video, "archive")
        except Exception as exc:
            print(f"[archive] notify_success failed: {exc}", flush=True)
        # zh-tw sibling — best-effort, non-fatal.
        zh = _find_archive_zh_sibling(eng)
        if zh is None:
            continue
        zh_dest = canonical_video.parent / f"{canonical_video.stem}{_ZH_SUFFIX}"
        try:
            shutil.copy2(zh, zh_dest)
            print(f"[archive] attached zh {show_title!r} → {zh_dest.name}", flush=True)
        except OSError as exc:
            print(f"[archive] zh attach failed for {canonical_video.name}: {exc}", flush=True)
