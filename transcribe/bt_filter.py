"""Per-torrent post-download LLM filter.

When a torrent finishes (no `.aria2` control files left under the
wrapper), call `filter_wrapper(wrapper)`. The pass does three things:

  1. ONE Haiku call decides: for each video file, which bundled English
     SRT (if any) should attach as its sibling subtitle. The listing
     given to the model includes cue count + time span for each .srt so
     forced / SDH / full-dialogue tracks are distinguishable even when
     filenames are identical or unhelpful (e.g. `1.srt` / `2.srt`,
     RARBG's `2_English.srt` + `3_English.srt`).

  2. Flatten: every video file is moved to the wrapper root regardless
     of original nesting (`Show.S01/Season 01/E01.mkv` → `Show.S01/E01.mkv`).
     Each video's **prior pipeline siblings** travel with it:
     `<stem>.srt` (annotated English from whisper / OS),
     `<stem>.zh-tw.srt` (Chinese sidecar), `<stem>.zh-tw.srt.error`
     (translation failure stamp). LLM-matched bundled SRTs are then
     copied to root, but they NEVER clobber an existing
     `<stem>.srt` — a pre-existing annotated copy wins.

  3. Delete everything else: any entry remaining at the wrapper root
     that isn't a video, a placed SRT, or aria2c's `.torrent` resume
     metadata is removed (Sample/, Subs/, .nfo, .exe, RARBG.txt,
     screenshots, __MACOSX, …). Whitelist-based — there's no per-junk-
     pattern list to maintain.

Hard safety net: a video file is NEVER deleted at the root level, even
if the keeper-set bookkeeping somehow missed it. Move collisions abort
the whole delete pass.

Idempotency: a `.filtered` sentinel is written on exit (even on partial
failure) so subsequent scan ticks skip the wrapper. Delete the sentinel
by hand to force a re-run.
"""
import os
import shutil
from pathlib import Path

from annotate import parse_srt
from claude_client import generate_json
from srt_source import stamp_source

SENTINEL_NAME = ".filtered"

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


def _pipeline_siblings(video: Path) -> list[Path]:
    """Files in video's parent directory that belong to the same video
    in transcribe's pipeline: prior whisper / OS / annotate output
    (`<stem>.srt`), Chinese sidecars (`<stem>.zh-tw.srt`), or failure
    stamps (`<stem>.zh-tw.srt.error`).

    Match rule: name starts with `<video.stem>.` AND contains `.srt`
    (either as suffix or before `.error`). Catches every pattern the
    rest of the pipeline currently writes; conservative on stems so
    e.g. `Show.S01E01.Director.Cut.mkv` doesn't sweep in `S01E01.srt`.
    """
    parent = video.parent
    prefix = video.stem + "."
    out = []
    try:
        entries = list(parent.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_file() or entry == video:
            continue
        name = entry.name
        if not name.startswith(prefix):
            continue
        # `.srt` covers .srt and .zh-tw.srt; `.srt.error` covers the
        # translator's failure stamp pattern.
        if name.endswith(".srt") or name.endswith(".srt.error"):
            out.append(entry)
    return out


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
    """One-shot LLM pass on a freshly-finished torrent.

    Idempotent — writes a sentinel on completion so subsequent calls
    bail. Delete the sentinel to force a re-run."""
    if not wrapper.is_dir():
        return
    sentinel = wrapper / SENTINEL_NAME
    if sentinel.exists():
        return

    short = wrapper.name[:40]
    tree = _build_tree(wrapper)
    if not tree:
        try:
            sentinel.touch()
        except OSError:
            pass
        return

    # ── 1. LLM: pick SRT for each video ────────────────────────────────
    prompt = _PROMPT_TEMPLATE.format(wrapper_name=wrapper.name, tree="\n".join(tree))
    try:
        result = generate_json(prompt, _SCHEMA, model=_MODEL, temperature=0.0)
    except Exception as e:
        print(f"[filter {short}] LLM call failed ({e}); writing sentinel to avoid retry storm", flush=True)
        try:
            sentinel.touch()
        except OSError:
            pass
        return

    # Bonus directories — featurettes / extras / behind-the-scenes etc.
    # Videos inside these are NOT flattened to root; the directories
    # themselves get rm-rf'd in step 4 along with everything else that's
    # not a keeper, so the bonus videos go down with the ship.
    bonus_dirs: list[Path] = []
    for rel in result.get("bonus_dirs", []):
        p = _safe_resolve(wrapper, rel)
        if p is None:
            print(f"[filter {short}] bonus dir bad path: {rel!r}", flush=True)
            continue
        if not p.is_dir():
            print(f"[filter {short}] bonus dir not a directory: {rel!r}", flush=True)
            continue
        # Defense: never accept the wrapper itself as a bonus dir.
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

    # Build name→srt mapping survived across the move pass below.
    # Indexed by video filename because we're about to relocate videos to
    # the wrapper root, so their absolute paths shift but their names
    # stay stable.
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

    # ── 2. Flatten: move every video + its pipeline-produced siblings ──
    #     to wrapper root.
    #
    # Sibling preservation is the load-bearing fix: a previously-processed
    # wrapper has `<stem>.srt` (annotated English from whisper / OS),
    # `<stem>.zh-tw.srt` (Chinese sidecar), and possibly
    # `<stem>.zh-tw.srt.error` next to the video. Without this step, the
    # delete pass downstream wipes the nested directory along with all of
    # them — destroying the work the user has already paid for.
    keepers: set[Path] = set()  # resolved absolute paths NOT to delete
    move_failures: list[str] = []

    def _move(src: Path, label: str) -> bool:
        """Move src to wrapper/src.name, updating keepers + move_failures.
        Returns True on success (or if already at root, which counts)."""
        dst = wrapper / src.name
        if dst.resolve() == src.resolve():
            keepers.add(dst.resolve())
            return True
        if dst.exists():
            move_failures.append(f"{label} collision: {src.relative_to(wrapper)}")
            keepers.add(src.resolve())
            return False
        try:
            shutil.move(str(src), str(dst))
            keepers.add(dst.resolve())
            print(f"[filter {short}] flatten {label} {src.relative_to(wrapper)} → {dst.name}", flush=True)
            return True
        except OSError as e:
            move_failures.append(f"{label} move failed for {src.relative_to(wrapper)}: {e}")
            keepers.add(src.resolve())
            return False

    videos = [p for p in wrapper.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    # Defense: if every video lives inside a flagged bonus dir, the LLM
    # almost certainly mis-classified — fall back to treating nothing as
    # bonus so we don't end up with an empty wrapper.
    main_videos = [v for v in videos if not _under_bonus(v)]
    if not main_videos and videos:
        print(f"[filter {short}] every video falls in a bonus_dir — ignoring bonus_dirs (LLM likely wrong)", flush=True)
        bonus_dirs_set = set()
        main_videos = videos

    for video in main_videos:
        siblings = _pipeline_siblings(video)
        if not _move(video, "video"):
            # If the video itself can't move, leave its siblings alone too.
            continue
        for sib in siblings:
            _move(sib, "sibling")

    # Move collisions are rare in practice (release groups don't ship
    # two `E01.mkv` files) but if they happen, abort the destructive
    # delete pass — bad delete state is worse than no cleanup.
    if move_failures:
        print(f"[filter {short}] {len(move_failures)} flatten failure(s) — skipping delete pass:", flush=True)
        for msg in move_failures:
            print(f"  - {msg}", flush=True)
        try:
            sentinel.touch()
        except OSError:
            pass
        return

    # ── 3. Copy LLM-matched SRTs to root as <video stem>.srt ───────────
    # Anything that already lives at root (because step 2 moved a prior
    # pipeline output there, or because the file was already strict-stem
    # to begin with) wins over the LLM's pick — we never clobber.
    for video_name, srt_src in srt_by_video_name.items():
        video_at_root = wrapper / video_name
        if not video_at_root.exists():
            continue
        target_srt = video_at_root.with_suffix(".srt")
        if target_srt.exists():
            # Either the strict-stem layout (LLM and target are the same
            # file) or pre-existing pipeline output we just moved here.
            # Either way: don't clobber, don't restamp (it may already
            # carry `※ annotated` / a prior `※ source: ...` cue we don't
            # want to duplicate).
            keepers.add(target_srt.resolve())
            continue
        try:
            shutil.copy2(srt_src, target_srt)
            stamp_source(target_srt, "bundled-filter")
            keepers.add(target_srt.resolve())
            print(f"[filter {short}] srt {srt_src.relative_to(wrapper)} → {target_srt.name}", flush=True)
        except OSError as e:
            print(f"[filter {short}] srt copy failed: {e}", flush=True)

    # Preserve aria2c's seed-resume metadata files at root.
    for entry in wrapper.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".torrent":
            keepers.add(entry.resolve())

    # ── 4. Whitelist delete: everything at root not in keepers ─────────
    for entry in list(wrapper.iterdir()):
        if entry.resolve() in keepers:
            continue
        # Hard safety: never unlink a video at root, even if it somehow
        # missed the keeper set (defense against a buggy upstream).
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXTS:
            print(f"[filter {short}] hard safety: refusing to delete video {entry.name}", flush=True)
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
                print(f"[filter {short}] rm -rf {entry.name}", flush=True)
            elif entry.is_file():
                entry.unlink()
                print(f"[filter {short}] unlink {entry.name}", flush=True)
        except OSError as e:
            print(f"[filter {short}] delete failed for {entry.name}: {e}", flush=True)

    try:
        sentinel.touch()
    except OSError:
        pass
