"""Per-torrent post-download LLM filter.

Reads from /bt/<wrapper>/ (aria2's download dir — NEVER touched), writes
canonical Movies/TV hardlinks under /artifact/. Bundled subtitles are
NOT touched here — the downstream pipeline scans /bt for them at
whisper-completion time and content-matches via WER (see
tasks._pick_bundled).

When a torrent finishes (no `.aria2` control files left under the
wrapper), call `filter_wrapper(wrapper)`. The pass does two things:

  1. ONE Haiku call decides per video:
       - canonical Jellyfin-friendly title / year / (season, episode for TV)
       - which directories are bonus content (featurettes, behind-the-
         scenes, deleted scenes, etc.) so their videos get skipped

  2. Hardlink main-feature videos to canonical Movies/TV paths
     (Movies/Title (Year)/Title (Year).mkv,
      TV/Title (Year)/Season NN/Title (Year) - SNNENN.mkv).
     Same inode as the bt-side file; zero extra disk; aria2 keeps the
     original. The canonical /artifact/.../<stem>.srt is written ONLY
     by the pipeline, after annotation finishes.

There is NO delete pass — the bt-side wrapper is read-only to us. Junk
files (Sample/, Subs/, .nfo, RARBG.txt) stay in /bt/ for aria2 to keep
seeding from, and get cleaned up when the whole wrapper is removed by
the user (or by an aria2 seed-limit script later).

Bonus directories' videos are skipped from main_features so they never
land in /artifact. The /bt/ side keeps them for completeness.

Idempotency: `.filtered` sentinel is written to /artifact/_processed/
on exit (even on partial failure) so subsequent scan ticks skip the
wrapper. Delete the sentinel to force a re-run; existing hardlinks are
NOT overwritten.
"""
import os
import re
import shutil
from pathlib import Path

from claude_client import generate_json

SENTINEL_NAME = ".filtered"

# The two top-level roots, set as module constants so callers and tests
# can override (e.g. `bt_filter.ARTIFACT_ROOT = Path('/tmp/x')`). Both
# live under one bind mount (/app/data) so os.link() can hardlink across
# them inside the container — see docker-compose.yml for why.
BT_ROOT = Path("/app/data/bt")
ARTIFACT_ROOT = Path("/app/data/artifact")

# Sentinels for "this bt wrapper has been LLM-filtered, skip" live here
# rather than alongside the canonical Movies/TV output, because canonical
# paths derive from LLM-decided titles and the bt-wrapper name doesn't
# embed into them. One file per bt wrapper, named after the wrapper.
PROCESSED_DIR = ARTIFACT_ROOT / "_processed"

# Raw subtitle candidates mirror the canonical Movies/TV tree. Pipeline
# writes whisper output + downloaded OS candidates + bundled-SRT copies
# here; the verifier picks a winner and the winner is promoted to the
# canonical path. Keeping originals around means changing the verifier
# (WER threshold, new check) lets us replay without re-downloading or
# re-running whisper. See `_sources_path` for the per-video layout.
SOURCES_DIR = ARTIFACT_ROOT / "_sources"

# Filesystem-unsafe characters that need stripping from LLM-returned
# titles before they become path segments.
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}

# Cap prompt size so an absurdly large or hostile torrent layout can't
# blow the model's context. 200 entries covers a TV box-set season pack
# with multilingual Subs/ subfolders comfortably.
MAX_TREE_ENTRIES = 200

_MODEL = os.environ.get("ANTHROPIC_FILTER_MODEL", "claude-haiku-4-5-20251001")

_SCHEMA = {
    "type": "object",
    "properties": {
        "main_features": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video": {"type": "string", "description": "relative path of the video file inside the wrapper"},
                    "kind": {"type": "string", "enum": ["movie", "tv"]},
                    "title": {"type": "string", "description": "canonical Jellyfin-friendly title, with release-group / quality / source junk stripped (e.g. 'The Sopranos', 'Spider-Man: Into the Spider-Verse')"},
                    "year": {"type": "integer", "description": "original release year (movie: theatrical release year; tv: series first-air year, NOT the season's year)"},
                    "season": {"type": "integer", "description": "TV only. Season number as a positive integer. Omit for movies."},
                    "episode": {"type": "integer", "description": "TV only. Episode number within the season. Omit for movies."},
                },
                "required": ["video", "kind", "title", "year"],
            },
        },
        "bonus_dirs": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "relative path of a directory whose video contents are bonus material (featurettes / behind-the-scenes / interviews / extras / trailers), not the main feature; videos inside are excluded from main_features so they never land in /artifact and stay only on the bt side",
            },
        },
    },
    "required": ["main_features", "bonus_dirs"],
}


_PROMPT_TEMPLATE = """\
A freshly-downloaded BT torrent. Two decisions:

A) For each MAIN-FEATURE video, provide its canonical Jellyfin-friendly \
naming so the library entry doesn't depend on the release group's quirks:
   - kind: "movie" or "tv"
   - title: clean canonical title with release-group / quality / source \
junk stripped. Use the form a media database (TMDB/Wikipedia) would use: \
"Spider-Man: Into the Spider-Verse" not "Spider.Man.Into.The.Spider.Verse". \
Keep the colon / apostrophe / ampersand if the canonical title has them — \
filesystem sanitisation happens downstream.
   - year: the original release year as an integer. For TV, this is the \
SERIES FIRST-AIR year, not the season's year (e.g. The Sopranos season 3 \
released 2001 but year=1999).
   - For TV only: include `season` and `episode`. S01E01 → season=1, \
episode=1. Always emit both for TV files.

   Use your knowledge of mainstream titles. For any title you're \
not confident about — especially recent releases that may post-date \
your training data — use the web_search tool to look up the canonical \
title and first-air / release year before answering. A wrong year \
breaks Jellyfin's metadata lookup downstream. Skip bonus / extras \
videos (they're handled in B below).

B) Identify BONUS-CONTENT directories whose videos are NOT the main \
feature. List their relative paths in `bonus_dirs`. Typical names:
   - Featurettes / Featurette
   - Extras / Bonus / Bonus Content / Bonus Features
   - Behind the Scenes / Making Of
   - Deleted Scenes / Bloopers / Outtakes
   - Trailers / Interviews / Commentary

   Only directories whose contents are CLEARLY bonus material. Do NOT \
list directories that contain the main episodes (`Season 01`, the \
release group's wrapper folder, `Subs`, etc.). If unsure, omit.

Bundled subtitle files (`.srt`) in the listing are NOT your concern —
the downstream pipeline runs whisper then content-matches each `.srt`
against whisper output via WER. You only need to classify videos.

Torrent: {wrapper_name}

Listing (paths relative to torrent folder; sizes shown):

{tree}

Return paths exactly as they appear above (relative, slash-separated).
"""


def _human_size(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}G"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n}B"


def _build_tree(wrapper: Path) -> list[str]:
    """Walk wrapper depth-first, return human-readable listing lines.

    Lists folders and files (with sizes) so the LLM has the structural
    context it needs to spot bonus directories. SRT cue counts /
    previews are deliberately omitted — Haiku doesn't pick subtitles
    any more; the downstream WER gate decides which `.srt` matches
    which video by content."""
    lines: list[str] = []
    count = 0
    for entry in sorted(wrapper.rglob("*")):
        if count >= MAX_TREE_ENTRIES:
            lines.append("...(truncated)")
            break
        try:
            rel = entry.relative_to(wrapper)
        except ValueError:
            continue
        if entry.is_dir():
            lines.append(f"D {rel}/")
        elif entry.is_file():
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            lines.append(f"F {rel}  {_human_size(size)}")
        count += 1
    return lines


def _safe_title(title: str) -> str:
    """Strip filesystem-unsafe characters from an LLM-returned title.
    Colons / slashes / etc. get dropped; whitespace collapses. Returns a
    fallback 'Untitled' if the result is empty so callers always have a
    usable path segment."""
    s = _INVALID_FS_CHARS.sub("", title).strip()
    s = re.sub(r"\s+", " ", s)
    return s or "Untitled"


def _canonical_path(entry: dict, ext: str) -> Path | None:
    """Build the /artifact canonical path for one main_features entry.
    Returns None if the entry is malformed (missing required fields,
    bad types, unknown kind).

    Movies → /artifact/Movies/Title (Year)/Title (Year).ext
    TV     → /artifact/TV/Title (Year)/Season NN/Title (Year) - SNNENN.ext
    """
    try:
        kind = entry["kind"]
        title = _safe_title(entry["title"])
        year = int(entry["year"])
    except (KeyError, ValueError, TypeError):
        return None

    base = f"{title} ({year})"

    if kind == "movie":
        return ARTIFACT_ROOT / "Movies" / base / f"{base}{ext}"

    if kind == "tv":
        try:
            season = int(entry["season"])
            episode = int(entry["episode"])
        except (KeyError, ValueError, TypeError):
            return None
        return (
            ARTIFACT_ROOT
            / "TV"
            / base
            / f"Season {season:02d}"
            / f"{base} - S{season:02d}E{episode:02d}{ext}"
        )

    return None


def _sources_path(canonical_video: Path, source_tag: str) -> Path:
    """Map a canonical video path to its `_sources/` mirror entry for a
    given source tag. Mirrors `_canonical_path` for the source-staging
    tree:

      /artifact/Movies/Title (Year)/Title (Year).mkv
        →  /artifact/_sources/Movies/Title (Year)/Title (Year).<tag>.srt

      /artifact/TV/Title (Year)/Season 01/Title (Year) - S01E01.mkv
        →  /artifact/_sources/TV/Title (Year)/Season 01/Title (Year) - S01E01.<tag>.srt

    `source_tag` is one of: "whisper", "embedded", "pgs-ocr", "bundled",
    "opensubtitles-hash", "opensubtitles-text", "verified". The tag
    becomes part of the filename so multiple candidates for the same
    video sit side-by-side and can be inspected with `ls`.
    """
    try:
        rel = canonical_video.relative_to(ARTIFACT_ROOT)
    except ValueError:
        # Canonical path didn't start under /artifact — defensive fallback
        # (the rest of the pipeline only ever passes paths produced by
        # `_canonical_path`, so this branch should not trigger).
        return SOURCES_DIR / f"{canonical_video.stem}.{source_tag}.srt"
    return SOURCES_DIR / rel.parent / f"{canonical_video.stem}.{source_tag}.srt"


def _sentinel_for(wrapper_name: str) -> Path:
    """Sentinel file path for a bt wrapper, in /artifact/_processed/.
    Wrapper-name sanitisation matches _safe_title's so different wrapper
    names can't collide (rare in practice, defence in depth)."""
    safe = _INVALID_FS_CHARS.sub("_", wrapper_name)[:180] or "wrapper"
    return PROCESSED_DIR / f"{safe}.filtered"


def load_manifest(wrapper_name: str) -> list[Path]:
    """Return the absolute canonical /artifact paths bt_filter produced
    for this bt wrapper, parsed from the sentinel file contents.

    Used by per-torrent UI actions (translate-zh / upgrade-english) so a
    click on a bt-side torrent name reaches the right canonical-named
    files even though the canonical names don't carry wrapper info.

    Returns an empty list if the sentinel doesn't exist or the wrapper
    produced no canonical outputs (empty tree, LLM error, etc.)."""
    sentinel = _sentinel_for(wrapper_name)
    if not sentinel.is_file():
        return []
    try:
        text = sentinel.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Path] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Defensive: reject relative escapes (..) so a corrupted sentinel
        # can't surface paths outside /artifact.
        candidate = (ARTIFACT_ROOT / line).resolve()
        try:
            candidate.relative_to(ARTIFACT_ROOT.resolve())
        except ValueError:
            continue
        out.append(candidate)
    return out


def _write_sentinel(wrapper_name: str, canonical_videos: list[Path]) -> None:
    """Touch /artifact/_processed/<wrapper>.filtered with the canonical
    video paths bt_filter just produced, one per line. Caller passes the
    main-feature target paths (videos, not SRTs — SRTs are derivable
    via with_suffix('.srt'))."""
    sentinel = _sentinel_for(wrapper_name)
    try:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            str(p.relative_to(ARTIFACT_ROOT))
            for p in canonical_videos
        )
        sentinel.write_text(body + ("\n" if body else ""), encoding="utf-8")
    except OSError:
        pass


def _safe_resolve(wrapper: Path, rel: str) -> Path | None:
    """Resolve `rel` strictly under wrapper. Returns None on path escape."""
    if not rel:
        return None
    try:
        target = (wrapper / rel).resolve()
        target.relative_to(wrapper.resolve())
        return target
    except (ValueError, OSError):
        return None


def filter_wrapper(wrapper: Path) -> None:
    """One-shot LLM pass on a freshly-finished torrent. Reads from
    `wrapper` (under /bt/), writes to canonical paths under /artifact/:

      Movies → /artifact/Movies/Title (Year)/Title (Year).ext
      TV     → /artifact/TV/Title (Year)/Season NN/Title (Year) - SNNENN.ext

    The bt-side wrapper is NEVER modified.

    Idempotent — writes /artifact/_processed/<wrapper>.filtered on
    completion so subsequent calls bail. Delete that sentinel to force a
    re-run; existing canonical hardlinks + SRTs are preserved
    (annotation work survives)."""
    if not wrapper.is_dir():
        return

    sentinel = _sentinel_for(wrapper.name)
    if sentinel.exists():
        return

    short = wrapper.name[:40]
    tree = _build_tree(wrapper)
    if not tree:
        _write_sentinel(wrapper.name, [])
        return

    # ── 1. LLM: canonical naming + bonus-dir classification ──────────
    prompt = _PROMPT_TEMPLATE.format(wrapper_name=wrapper.name, tree="\n".join(tree))
    try:
        result = generate_json(prompt, _SCHEMA, model=_MODEL, temperature=0.0,
                               web_search=True)
    except Exception as e:
        print(f"[filter {short}] LLM call failed ({e}); writing sentinel to avoid retry storm", flush=True)
        _write_sentinel(wrapper.name, [])
        return

    # ── 2. For each main feature: hardlink video to canonical path ────
    # No bundled-SRT copying here — the downstream pipeline scans /bt/ at
    # whisper-completion time and content-matches every `.srt` against
    # whisper output via WER. bt_filter's only job for subs is "stay out
    # of the way" (don't write to canonical, don't pre-pick a bundled).
    canonical_videos: list[Path] = []  # tracked for the sentinel manifest

    for entry in result.get("main_features", []):
        video_rel = entry.get("video", "")
        video_p = _safe_resolve(wrapper, video_rel)
        if video_p is None or not video_p.is_file():
            print(f"[filter {short}] main_feature bad path: {video_rel!r}", flush=True)
            continue
        if video_p.suffix.lower() not in VIDEO_EXTS:
            print(f"[filter {short}] main_feature not a video: {video_p.name}", flush=True)
            continue

        target_video = _canonical_path(entry, video_p.suffix)
        if target_video is None:
            print(f"[filter {short}] could not build canonical path for {entry}", flush=True)
            continue

        try:
            target_video.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"[filter {short}] mkdir failed for {target_video.parent}: {e}", flush=True)
            continue

        # Hardlink video. Same inode as /bt/<wrapper>/.../<video>; zero
        # extra disk; aria2 keeps seeing the original. Pre-existing target
        # from a prior run is left alone.
        if not target_video.exists():
            try:
                os.link(str(video_p), str(target_video))
                print(f"[filter {short}] hardlink {video_p.relative_to(wrapper)} → {target_video.relative_to(ARTIFACT_ROOT)}", flush=True)
            except OSError as e:
                try:
                    shutil.copy2(str(video_p), str(target_video))
                    print(f"[filter {short}] hardlink failed ({e}); copied → {target_video.relative_to(ARTIFACT_ROOT)}", flush=True)
                except OSError as e2:
                    print(f"[filter {short}] both link+copy failed: {e}, {e2}", flush=True)
                    continue

        # Track for the manifest regardless of whether we just created it
        # or it pre-existed; per-torrent UI actions need every produced
        # video, not only the new ones.
        canonical_videos.append(target_video)

    # ── 3. Sentinel + manifest ────────────────────────────────────────
    # No delete pass — /bt/ is read-only to us. Bonus content (videos
    # not in main_features) simply doesn't get hardlinked; it stays in
    # /bt for aria2 to keep seeding and the user can remove the bt-side
    # wrapper when they're done with the torrent.
    _write_sentinel(wrapper.name, canonical_videos)
