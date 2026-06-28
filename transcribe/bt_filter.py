"""Per-torrent post-download LLM filter.

Reads from /bt/<wrapper>/ (aria2's download dir — NEVER touched), writes
to /artifact/<wrapper>/. Separation keeps aria2's seeding files intact
and makes deletion / cleanup of either side independently safe.

When a torrent finishes (no `.aria2` control files left under the
wrapper), call `filter_wrapper(wrapper)`. The pass does three things:

  1. ONE Haiku call decides: for each video file, which bundled English
     SRT (if any) should attach as its sibling subtitle. The listing
     given to the model includes cue count + time span for each .srt so
     forced / SDH / full-dialogue tracks are distinguishable even when
     filenames are identical or unhelpful (e.g. `1.srt` / `2.srt`,
     RARBG's `2_English.srt` + `3_English.srt`).

  2. Hardlink main-feature videos to /artifact/<wrapper>/<filename>
     (flat — no nested season folders). Same inode as the bt-side file,
     zero extra disk, aria2 keeps seeing the original.

  3. Copy LLM-matched bundled SRTs to /artifact/<wrapper>/<stem>.srt
     (copy because we may stamp ※ markers into them later and we don't
     want that bleeding back into aria2's directory).

There is NO delete pass — the bt-side wrapper is read-only to us. Junk
files (Sample/, Subs/, .nfo, RARBG.txt) stay in /bt/ for aria2 to keep
seeding from, and get cleaned up when the whole wrapper is removed by
the user (or by an aria2 seed-limit script later).

Bonus directories (featurettes, behind-the-scenes) are still detected
by the LLM and their videos are skipped — they just don't get
hardlinked into /artifact/. The /bt/ side keeps them.

Idempotency: `.filtered` sentinel is written to /artifact/<wrapper>/
on exit (even on partial failure) so subsequent scan ticks skip the
wrapper. Delete the sentinel to force a re-run; existing hardlinks /
SRT files are NOT overwritten (they're load-bearing for already-
done whisper / ※ annotation / zh-translation work).
"""
import os
import shutil
from pathlib import Path

from annotate import parse_srt
from claude_client import generate_json
from srt_source import stamp_source

SENTINEL_NAME = ".filtered"

# The two top-level roots, set as module constants so callers and tests
# can override (e.g. `bt_filter.ARTIFACT_ROOT = Path('/tmp/x')`). Both
# live under one bind mount (/app/data) so os.link() can hardlink across
# them inside the container — see docker-compose.yml for why.
BT_ROOT = Path("/app/data/bt")
ARTIFACT_ROOT = Path("/app/data/artifact")

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}

# Cap prompt size so an absurdly large or hostile torrent layout can't
# blow the model's context. 200 entries covers a TV box-set season pack
# with multilingual Subs/ subfolders comfortably.
MAX_TREE_ENTRIES = 200

_MODEL = os.environ.get("ANTHROPIC_FILTER_MODEL", "claude-haiku-4-5-20251001")

_SCHEMA = {
    "type": "object",
    "properties": {
        "srt_matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video": {"type": "string", "description": "relative path of the video"},
                    "srt": {"type": "string", "description": "relative path of the English SRT to attach"},
                },
                "required": ["video", "srt"],
            },
        },
        "bonus_dirs": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "relative path of a directory whose video contents are bonus material (featurettes / behind-the-scenes / interviews / extras / trailers), not the main feature; videos inside will be skipped during flatten so they get removed with the directory",
            },
        },
    },
    "required": ["srt_matches", "bonus_dirs"],
}


_PROMPT_TEMPLATE = """\
A freshly-downloaded BT torrent. Two decisions:

A) For each MAIN-FEATURE video, pick the best English subtitle to \
attach as its sibling. Omit a video from srt_matches if no usable \
English SRT exists.

   Each SRT entry includes:
     [N cues, START → END]   — coverage stats
     preview: '<first cue>'  — first dialogue line, for language check

   Picking rules:
   - Prefer FULL dialogue tracks: hundreds of cues spanning the whole \
runtime (e.g. 00:00 → 00:55 on a 55-min show).
   - SDH (similar coverage with extra [MUSIC] / [DOOR SLAMS] cues) \
is an acceptable fallback.
   - AVOID FORCED tracks: very few cues (often ~10), narrow span — \
they only subtitle foreign-language scenes / signs.
   - The preview must look like real English dialogue, not Spanish \
/ French / Italian / Chinese.
   - Match per episode by number when the torrent is a season pack \
(S01E01.mkv ↔ Subs/01_English.srt etc.).

B) Identify BONUS-CONTENT directories whose videos are NOT the main \
feature and should be discarded with the directory itself. List their \
relative paths in `bonus_dirs`. Typical names:
   - Featurettes / Featurette
   - Extras / Bonus / Bonus Content / Bonus Features
   - Behind the Scenes / Making Of
   - Deleted Scenes / Bloopers / Outtakes
   - Trailers / Interviews / Commentary

   Only directories whose contents are CLEARLY bonus material. Do NOT \
list directories that contain the main episodes (`Season 01`, the \
release group's wrapper folder, `Subs`, etc.). If unsure, omit.

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


def _srt_stats_and_preview(p: Path) -> tuple[str | None, str | None]:
    """Return `(stats, preview)` for an SRT: cue count + time span, and
    the first cue's dialogue text. Either may be None if the file isn't
    parseable as SRT (binary subs, malformed, etc.)."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    try:
        cues = parse_srt(text)
    except Exception:
        return None, None
    if not cues:
        return None, None
    # Trim milliseconds from the timestamps to keep the listing compact.
    first_time = cues[0]["time"].split(" --> ")[0].split(",")[0]
    last_time = cues[-1]["time"].split(" --> ")[0].split(",")[0]
    stats = f"{len(cues)} cues, {first_time} → {last_time}"
    preview = " ".join(cues[0]["lines"]).strip()[:120] or None
    return stats, preview


def _build_tree(wrapper: Path) -> list[str]:
    """Walk wrapper depth-first, return human-readable listing lines."""
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
            line = f"F {rel}  {_human_size(size)}"
            if entry.suffix.lower() == ".srt":
                stats, preview = _srt_stats_and_preview(entry)
                if stats:
                    line += f"  [{stats}]"
                if preview:
                    line += f"  preview: {preview!r}"
            lines.append(line)
        count += 1
    return lines


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
    `wrapper` (under /bt/), writes to /artifact/<wrapper.name>/. The
    bt-side wrapper is NEVER modified.

    Idempotent — writes /artifact/<wrapper.name>/.filtered on completion
    so subsequent calls bail. Delete the sentinel to force a re-run;
    existing hardlinks + SRTs are preserved (annotation work survives)."""
    if not wrapper.is_dir():
        return

    artifact_dir = ARTIFACT_ROOT / wrapper.name
    sentinel = artifact_dir / SENTINEL_NAME
    if sentinel.exists():
        return

    short = wrapper.name[:40]
    tree = _build_tree(wrapper)
    if not tree:
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        except OSError:
            pass
        return

    # ── 1. LLM: pick SRT for each video + flag bonus dirs ──────────────
    prompt = _PROMPT_TEMPLATE.format(wrapper_name=wrapper.name, tree="\n".join(tree))
    try:
        result = generate_json(prompt, _SCHEMA, model=_MODEL, temperature=0.0)
    except Exception as e:
        print(f"[filter {short}] LLM call failed ({e}); writing sentinel to avoid retry storm", flush=True)
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        except OSError:
            pass
        return

    # Bonus directories — featurettes / extras / behind-the-scenes etc.
    # Videos inside these get SKIPPED during hardlink so the main feature
    # is what Jellyfin sees. The bonus videos stay in /bt/ where they
    # came from; the user can clean up the whole wrapper later.
    bonus_dirs: list[Path] = []
    for rel in result.get("bonus_dirs", []):
        p = _safe_resolve(wrapper, rel)
        if p is None:
            print(f"[filter {short}] bonus dir bad path: {rel!r}", flush=True)
            continue
        if not p.is_dir():
            print(f"[filter {short}] bonus dir not a directory: {rel!r}", flush=True)
            continue
        if p.resolve() == wrapper.resolve():
            print(f"[filter {short}] bonus dir is the wrapper root, ignoring", flush=True)
            continue
        bonus_dirs.append(p.resolve())
    bonus_dirs_set = set(bonus_dirs)

    def _under_bonus(video: Path) -> bool:
        video_r = video.resolve()
        for bd in bonus_dirs_set:
            try:
                video_r.relative_to(bd)
                return True
            except ValueError:
                continue
        return False

    # Build name → SRT-source-path mapping.
    srt_by_video_name: dict[str, Path] = {}
    for m in result.get("srt_matches", []):
        video_rel = m.get("video", "")
        srt_rel = m.get("srt", "")
        video_p = _safe_resolve(wrapper, video_rel)
        srt_p = _safe_resolve(wrapper, srt_rel)
        if video_p is None or srt_p is None:
            print(f"[filter {short}] bad match path: {m}", flush=True)
            continue
        if video_p.suffix.lower() not in VIDEO_EXTS:
            print(f"[filter {short}] not a video: {video_p.name}", flush=True)
            continue
        if srt_p.suffix.lower() != ".srt":
            print(f"[filter {short}] not an srt: {srt_p.name}", flush=True)
            continue
        if not video_p.is_file() or not srt_p.is_file():
            continue
        srt_by_video_name[video_p.name] = srt_p

    videos = [p for p in wrapper.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    # Defense: if every video lives inside a flagged bonus dir, the LLM
    # almost certainly mis-classified — fall back to treating nothing as
    # bonus so we don't end up with an empty artifact dir.
    main_videos = [v for v in videos if not _under_bonus(v)]
    if not main_videos and videos:
        print(f"[filter {short}] every video falls in a bonus_dir — ignoring bonus_dirs (LLM likely wrong)", flush=True)
        bonus_dirs_set = set()
        main_videos = videos

    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[filter {short}] could not create artifact dir: {e}", flush=True)
        return

    # ── 2. Hardlink main-feature videos into artifact_dir ──────────────
    # Same inode as /bt/<wrapper>/.../<video>; zero extra disk; aria2
    # keeps seeing the original. Flat at artifact root (drop nested
    # season folders), so Jellyfin scans cleanly.
    for video in main_videos:
        target = artifact_dir / video.name
        if target.exists():
            # Pre-existing from a prior run — don't re-link.
            continue
        try:
            os.link(str(video), str(target))
            print(f"[filter {short}] hardlink {video.relative_to(wrapper)} → {target.name}", flush=True)
        except OSError as e:
            # EXDEV (cross-filesystem) or other rare failure: fall back
            # to copy. Doubles disk usage but keeps the pipeline alive
            # on weird mount setups.
            try:
                shutil.copy2(str(video), str(target))
                print(f"[filter {short}] hardlink failed ({e}); copied instead → {target.name}", flush=True)
            except OSError as e2:
                print(f"[filter {short}] both hardlink and copy failed: {e}, {e2}", flush=True)

    # ── 3. Copy LLM-matched bundled SRTs ───────────────────────────────
    # Copy (not hardlink) because downstream will stamp ※ markers into
    # them and we don't want that bleeding back into /bt/. Never clobber
    # an existing /artifact/<wrapper>/<stem>.srt — that's already-done
    # whisper / annotate work the user has paid for.
    for video_name, srt_src in srt_by_video_name.items():
        target_video = artifact_dir / video_name
        if not target_video.exists():
            continue
        target_srt = target_video.with_suffix(".srt")
        if target_srt.exists():
            continue
        try:
            shutil.copy2(str(srt_src), str(target_srt))
            stamp_source(target_srt, "bundled-filter")
            print(f"[filter {short}] srt {srt_src.relative_to(wrapper)} → {target_srt.name}", flush=True)
        except OSError as e:
            print(f"[filter {short}] srt copy failed: {e}", flush=True)

    # ── 4. Sentinel ────────────────────────────────────────────────────
    # No delete pass — /bt/ is read-only to us. Junk (Sample/, .nfo,
    # bonus dirs) stays for aria2 to seed from until the user removes
    # the bt-side wrapper.
    try:
        sentinel.touch()
    except OSError:
        pass
