"""Per-torrent post-download LLM filter.

Reads from /bt/<wrapper>/ (aria2's download dir — NEVER touched), writes
canonical Movies/TV hardlinks under /artifact/. Bundled subtitles are
NOT touched here — the downstream pipeline scans /bt for them at
whisper-completion time and content-matches via WER (see
tasks._pick_bundled).

When a torrent finishes (no `.aria2` control files left under the
wrapper), call `filter_wrapper(wrapper)`. The pass does two things:

  1. ONE Opus call reads a compact summary of the wrapper (top-level
     folders, video counts, sample filenames) and decides in one of
     two modes:

     Mode "tv_regex" — the wrapper is a single-show TV pack (one
       season, or a whole-series complete pack). LLM returns the
       series title + first-air year + a Python regex that captures
       `season` and `episode` from any episode's filename via named
       groups. Code applies the regex to every video file in the
       wrapper (excluding bonus_dirs) and hardlinks. This handles
       arbitrarily large torrents (Friends full 10 seasons = ~240
       episodes) with constant LLM input/output size.

     Mode "per_video" — the wrapper is a movie, a small collection
       of unrelated films, or otherwise doesn't fit the single-show-
       regex mold. LLM returns a `main_features` array with one
       entry per main-feature video (canonical title / year / kind
       / optional season+episode). Code walks the list and hardlinks
       each entry.

     Both modes also return `bonus_dirs` — relative paths of
     directories whose videos are bonus content (featurettes,
     behind-the-scenes, etc.) and should NOT be hardlinked.

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
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup"}

# Cap the number of sample filenames per directory shown to the LLM.
# The smart summary already collapses "N videos in this dir" to a count
# plus a first + last sample; anything past 2-3 samples is diminishing
# returns for pattern detection. Kept as a constant for clarity.
_SAMPLES_PER_DIR = 2

_MODEL = os.environ.get("ANTHROPIC_FILTER_MODEL", "claude-opus-4-7")
_WEB_SEARCH_MAX_USES = 3

_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["tv_regex", "per_video"],
            "description": "'tv_regex' when the wrapper is a single-show TV pack "
                           "(one series, any number of seasons/episodes) and every "
                           "episode's filename can be captured by one regex. "
                           "'per_video' for movies, mixed collections, or TV packs "
                           "where filenames don't share a regex-parseable pattern.",
        },
        # tv_regex mode:
        "series_title": {
            "type": "string",
            "description": "TV_REGEX ONLY. Canonical Jellyfin-friendly show title, "
                           "release-group / quality / source junk stripped. "
                           "e.g. 'The Sopranos', 'Friends'. Empty string in per_video mode.",
        },
        "series_year": {
            "type": "integer",
            "description": "TV_REGEX ONLY. Series first-air year (NOT the season's "
                           "year — e.g. Sopranos S03 released 2001 but series_year=1999). "
                           "0 in per_video mode.",
        },
        "episode_regex": {
            "type": "string",
            "description": "TV_REGEX ONLY. Python regex applied to each video "
                           "filename's basename. MUST contain two named groups: "
                           "(?P<season>\\d+) and (?P<episode>\\d+). Example: "
                           "'(?i)S(?P<season>\\d{2})E(?P<episode>\\d{2})' captures "
                           "'S01E05' from 'Friends.S01E05.720p.mkv'. Empty string in "
                           "per_video mode.",
        },
        # per_video mode:
        "main_features": {
            "type": "array",
            "description": "PER_VIDEO ONLY. One entry per main-feature video. Empty "
                           "array in tv_regex mode.",
            "items": {
                "type": "object",
                "properties": {
                    "video": {"type": "string", "description": "relative path of the video file inside the wrapper"},
                    "kind": {"type": "string", "enum": ["movie", "tv"]},
                    "title": {"type": "string", "description": "canonical Jellyfin-friendly title"},
                    "year": {"type": "integer", "description": "original release year (movie: theatrical; tv: series first-air year)"},
                    "season": {"type": "integer", "description": "TV only. Season number. Omit for movies."},
                    "episode": {"type": "integer", "description": "TV only. Episode number. Omit for movies."},
                },
                "required": ["video", "kind", "title", "year"],
            },
        },
        # Both modes:
        "bonus_dirs": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "relative path of a directory whose video contents are bonus material (featurettes / behind-the-scenes / interviews / extras / trailers), not the main feature; videos inside are excluded from hardlinking so they never land in /artifact",
            },
        },
    },
    "required": ["mode", "series_title", "series_year", "episode_regex", "main_features", "bonus_dirs"],
}


_PROMPT_TEMPLATE = """\
A freshly-downloaded BT torrent. Classify it and provide the canonical \
Jellyfin-friendly naming.

Decide MODE first. Two mutually-exclusive options:

MODE = "tv_regex" — the wrapper contains episodes of ONE TV show \
(single season, single-show whole-series pack, etc.). All episode \
filenames follow one regex-parseable pattern. This mode is O(1) — \
you give one title/year/regex regardless of episode count, and the \
regex will be applied to every video by downstream code.
   Return:
   - series_title: canonical show title (Jellyfin/TMDB form — colons, \
apostrophes, ampersands preserved; release-group junk stripped)
   - series_year: SERIES FIRST-AIR year as an integer (NOT the \
season's year — e.g. Sopranos S03 came out 2001 but series_year=1999)
   - episode_regex: a Python regex string that applied to each \
episode's filename basename captures both `season` and `episode` as \
named groups. Recommended shape: '(?i)S(?P<season>\\d{{1,2}})E(?P<episode>\\d{{1,3}})'. \
Look at the sample filenames given below to see the actual pattern in \
use — if the release uses '01x05' style, adjust accordingly (e.g. \
'(?P<season>\\d{{1,2}})x(?P<episode>\\d{{1,3}})'). Make it as tight as \
practical so it doesn't over-match in bonus/misc filenames.
   - main_features: leave as [] (empty)

MODE = "per_video" — the wrapper is one or more MOVIES, or a mixed \
collection where filenames don't share a regex-parseable pattern. \
This is the legacy per-video enumeration.
   Return:
   - main_features: one entry per main-feature video: \
{{"video": "<rel path>", "kind": "movie"|"tv", "title": "...", \
"year": <int>, ["season": <int>, "episode": <int>]}}
   - series_title, series_year, episode_regex: leave empty ("", 0, "")

Both modes also need:
   - bonus_dirs: relative paths of BONUS-CONTENT directories whose \
videos should NOT be hardlinked. Typical names: Featurettes, Extras, \
Bonus, Behind the Scenes, Making Of, Deleted Scenes, Bloopers, \
Outtakes, Trailers, Interviews, Commentary. If a bonus filename lives \
next to the main episodes (not in its own directory), your \
episode_regex should not accidentally match it — otherwise it will \
hardlink and clutter the Jellyfin library.

If you're not sure about the canonical title / year of anything — \
especially recent releases past your training cutoff — USE WEB SEARCH. \
A wrong year breaks Jellyfin metadata lookup downstream, and \
false-confidence guesses have caused real user damage.

Bundled subtitle files (`.srt`) are NOT your concern — the downstream \
pipeline content-matches them via WER. You only classify videos.

Torrent: {wrapper_name}

Structural summary (directories collapsed with counts + sample filenames):

{tree}

Return relative paths exactly as they appear in the summary above.
"""


def _human_size(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}G"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n}B"


def _collect_videos(root: Path) -> list[Path]:
    """Every file under `root` whose suffix is a recognized video type,
    sorted alphabetically. Sort matters: sample-picking takes first +
    last, and pattern-detection wants those to be actual boundaries
    (E01 + E24), not filesystem-order accidents."""
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


def _pick_samples(files: list[Path]) -> list[Path]:
    """First + last (alphabetical) for pattern detection — a matched
    pair reveals the varying middle segment (S01E01 vs S01E24). Never
    more than `_SAMPLES_PER_DIR` entries."""
    if len(files) <= _SAMPLES_PER_DIR:
        return files
    return [files[0], files[-1]]


def _smart_tree_summary(wrapper: Path) -> str:
    """Compact structural view of `wrapper` for the LLM prompt.

    Output stays O(#directories) rather than O(#files) — a full 10-season
    Friends pack (240+ episodes) renders in ~30 lines, same as a
    single-episode torrent. Directories with lots of videos get collapsed
    to a count + 2 sample filenames (first + last, which lets the LLM
    infer the SxxExx pattern without seeing every episode).

    Recursion is one level deep from wrapper — enough for the common
    Season 01/ vs Complete/Season 01/ layouts. Deeper nesting still gets
    counted (via rglob) but only the outer summary line is emitted."""
    lines = [f"Wrapper: {wrapper.name}", "Contents:"]

    try:
        top_entries = sorted(wrapper.iterdir())
    except OSError:
        return "\n".join(lines)

    for entry in top_entries:
        rel = entry.relative_to(wrapper)
        if entry.is_file():
            try:
                size = _human_size(entry.stat().st_size)
            except OSError:
                size = "?"
            lines.append(f"  {rel}  ({size})")
            continue

        if entry.is_dir():
            _summarize_dir(entry, wrapper, lines, depth=1)

    return "\n".join(lines)


def _summarize_dir(subdir: Path, wrapper: Path, lines: list[str], depth: int) -> None:
    """Emit lines describing `subdir`: video count, sample filenames,
    and (up to `depth`≤2) recurse into subdirs that themselves contain
    videos. Bounded recursion keeps output size predictable."""
    rel = subdir.relative_to(wrapper)
    indent = "  " * depth
    videos = _collect_videos(subdir)
    try:
        srt_count = sum(
            1 for p in subdir.rglob("*")
            if p.is_file() and p.suffix.lower() in SUBTITLE_EXTS
        )
    except OSError:
        srt_count = 0

    summary_parts = []
    if videos:
        summary_parts.append(f"{len(videos)} video")
        if len(videos) > 1:
            summary_parts[-1] += "s"
    if srt_count:
        summary_parts.append(f"{srt_count} srt")
    summary = ", ".join(summary_parts) if summary_parts else "empty"

    lines.append(f"{indent}{rel}/  ({summary})")

    for sample in _pick_samples(videos):
        lines.append(f"{indent}  sample: {sample.relative_to(subdir)}")

    # Drill one level deeper into subdirs that contain videos — captures
    # the Complete/Season 01/ case where the season dirs sit one level in.
    if depth < 2:
        try:
            children = sorted(subdir.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and _collect_videos(child):
                _summarize_dir(child, wrapper, lines, depth + 1)


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


def _under_bonus(rel_video: Path, bonus_rels: list[Path]) -> bool:
    """True if `rel_video` sits inside any of the bonus-directory
    relative paths."""
    for bonus in bonus_rels:
        try:
            rel_video.relative_to(bonus)
            return True
        except ValueError:
            continue
    return False


def _validated_regex(pattern: str, samples: list[Path]) -> re.Pattern | None:
    """Compile `pattern` and confirm it has the required named groups
    AND matches at least one of the sample basenames with integer
    captures. Returns the compiled Pattern on success, None otherwise —
    caller stamps an empty sentinel on None."""
    if not pattern:
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    if "season" not in compiled.groupindex or "episode" not in compiled.groupindex:
        return None
    for sample in samples:
        m = compiled.search(sample.name)
        if m is None:
            continue
        try:
            int(m.group("season"))
            int(m.group("episode"))
        except (ValueError, TypeError):
            continue
        return compiled
    return None


def _hardlink_one(video_p: Path, target_video: Path, wrapper: Path, short: str) -> bool:
    """Hardlink `video_p` → `target_video`, falling back to copy on
    cross-device failure. Returns True on success. Pre-existing target
    is treated as success (idempotent replay)."""
    if target_video.exists():
        return True
    try:
        target_video.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[filter {short}] mkdir failed for {target_video.parent}: {e}", flush=True)
        return False
    try:
        os.link(str(video_p), str(target_video))
        print(f"[filter {short}] hardlink {video_p.relative_to(wrapper)} → {target_video.relative_to(ARTIFACT_ROOT)}", flush=True)
        return True
    except OSError as e:
        try:
            shutil.copy2(str(video_p), str(target_video))
            print(f"[filter {short}] hardlink failed ({e}); copied → {target_video.relative_to(ARTIFACT_ROOT)}", flush=True)
            return True
        except OSError as e2:
            print(f"[filter {short}] both link+copy failed: {e}, {e2}", flush=True)
            return False


def _hardlink_tv_by_regex(
    wrapper: Path,
    regex: re.Pattern,
    title: str,
    year: int,
    bonus_rels: list[Path],
    short: str,
) -> list[Path]:
    """Walk every video under `wrapper`, apply `regex` to each basename,
    hardlink matches to canonical TV path via (season, episode). Videos
    inside `bonus_rels` are skipped. Returns list of produced canonical
    video paths (including pre-existing ones, for manifest completeness)."""
    canonical_videos: list[Path] = []
    for video_p in _collect_videos(wrapper):
        rel = video_p.relative_to(wrapper)
        if _under_bonus(rel, bonus_rels):
            continue
        m = regex.search(video_p.name)
        if m is None:
            print(f"[filter {short}] regex miss: {rel}", flush=True)
            continue
        try:
            season = int(m.group("season"))
            episode = int(m.group("episode"))
        except (ValueError, TypeError):
            print(f"[filter {short}] regex captured non-int for {rel}", flush=True)
            continue
        entry = {
            "kind": "tv",
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
        }
        target_video = _canonical_path(entry, video_p.suffix)
        if target_video is None:
            print(f"[filter {short}] could not build canonical path for {rel}", flush=True)
            continue
        if _hardlink_one(video_p, target_video, wrapper, short):
            canonical_videos.append(target_video)
    return canonical_videos


def _hardlink_per_video(
    wrapper: Path,
    main_features: list[dict],
    bonus_rels: list[Path],
    short: str,
) -> list[Path]:
    """Legacy per-video path: iterate LLM-returned main_features[],
    hardlink each. bonus_rels are also honored — LLM may have listed a
    bonus video in main_features by mistake, or a bonus_dir path may
    overlap; skip to be safe."""
    canonical_videos: list[Path] = []
    for entry in main_features:
        video_rel = entry.get("video", "")
        video_p = _safe_resolve(wrapper, video_rel)
        if video_p is None or not video_p.is_file():
            print(f"[filter {short}] main_feature bad path: {video_rel!r}", flush=True)
            continue
        if video_p.suffix.lower() not in VIDEO_EXTS:
            print(f"[filter {short}] main_feature not a video: {video_p.name}", flush=True)
            continue
        rel = video_p.relative_to(wrapper)
        if _under_bonus(rel, bonus_rels):
            print(f"[filter {short}] main_feature is under bonus_dir, skipping: {rel}", flush=True)
            continue
        target_video = _canonical_path(entry, video_p.suffix)
        if target_video is None:
            print(f"[filter {short}] could not build canonical path for {entry}", flush=True)
            continue
        if _hardlink_one(video_p, target_video, wrapper, short):
            canonical_videos.append(target_video)
    return canonical_videos


def filter_wrapper(wrapper: Path) -> None:
    """One-shot LLM pass on a freshly-finished torrent. Reads from
    `wrapper` (under /bt/), writes to canonical paths under /artifact/:

      Movies → /artifact/Movies/Title (Year)/Title (Year).ext
      TV     → /artifact/TV/Title (Year)/Season NN/Title (Year) - SNNENN.ext

    The bt-side wrapper is NEVER modified.

    Idempotent — writes /artifact/_processed/<wrapper>.filtered on
    completion so subsequent calls bail. Delete that sentinel to force a
    re-run; existing canonical hardlinks + SRTs are preserved
    (annotation work survives).

    Failure handling: LLM API error / schema mismatch / regex validation
    fail → write an empty sentinel (same as pre-refactor behavior) so
    the wrapper isn't retried in a scan-tick loop. Delete the sentinel
    manually to force re-run once the underlying cause is fixed."""
    if not wrapper.is_dir():
        return

    sentinel = _sentinel_for(wrapper.name)
    if sentinel.exists():
        return

    short = wrapper.name[:40]
    tree_text = _smart_tree_summary(wrapper)
    if not _collect_videos(wrapper):
        # No videos at all — write empty sentinel, nothing to do.
        _write_sentinel(wrapper.name, [])
        return

    # ── 1. LLM: mode dispatch + canonical naming ─────────────────────
    prompt = _PROMPT_TEMPLATE.format(wrapper_name=wrapper.name, tree=tree_text)
    try:
        result = generate_json(
            prompt, _SCHEMA,
            model=_MODEL,
            temperature=0.0,
            web_search=True,
            web_search_max_uses=_WEB_SEARCH_MAX_USES,
        )
    except Exception as e:
        print(f"[filter {short}] LLM call failed ({e}); writing empty sentinel", flush=True)
        _write_sentinel(wrapper.name, [])
        return

    mode = result.get("mode")
    bonus_rels: list[Path] = []
    for rel_str in result.get("bonus_dirs") or []:
        p = _safe_resolve(wrapper, rel_str)
        if p is None:
            continue
        try:
            bonus_rels.append(p.relative_to(wrapper))
        except ValueError:
            continue

    # ── 2. Dispatch by mode ──────────────────────────────────────────
    if mode == "tv_regex":
        title = (result.get("series_title") or "").strip()
        try:
            year = int(result.get("series_year") or 0)
        except (TypeError, ValueError):
            year = 0
        pattern = result.get("episode_regex") or ""
        if not title or year <= 0 or not pattern:
            print(f"[filter {short}] tv_regex mode missing series_title / year / regex; empty sentinel", flush=True)
            _write_sentinel(wrapper.name, [])
            return
        # Validate regex against actual filenames — bail if it doesn't
        # capture season/episode as integers on at least one sample.
        # Using first-few videos as validation samples: cheap + covers
        # the common cases without a full-tree pre-walk.
        samples = _collect_videos(wrapper)[:8]
        regex = _validated_regex(pattern, samples)
        if regex is None:
            print(f"[filter {short}] tv_regex validation failed for pattern {pattern!r}; empty sentinel", flush=True)
            _write_sentinel(wrapper.name, [])
            return
        print(f"[filter {short}] tv_regex mode: {title} ({year}) pattern={pattern!r}", flush=True)
        canonical_videos = _hardlink_tv_by_regex(wrapper, regex, title, year, bonus_rels, short)

    elif mode == "per_video":
        main_features = result.get("main_features") or []
        print(f"[filter {short}] per_video mode: {len(main_features)} entries", flush=True)
        canonical_videos = _hardlink_per_video(wrapper, main_features, bonus_rels, short)

    else:
        print(f"[filter {short}] unknown mode {mode!r}; empty sentinel", flush=True)
        _write_sentinel(wrapper.name, [])
        return

    # ── 3. Sentinel + manifest ────────────────────────────────────────
    # No delete pass — /bt/ is read-only to us. Bonus content (videos
    # not hardlinked) simply doesn't get hardlinked; it stays in /bt for
    # aria2 to keep seeding and the user can remove the bt-side wrapper
    # when they're done with the torrent.
    _write_sentinel(wrapper.name, canonical_videos)
