"""Per-torrent LLM pairing.

For each freshly-downloaded wrapper in `data/bt/`, one Haiku call decides:
for each main-feature video, which bundled English SRT (if any) is its
sibling. Returns `[Pairing(stem, video_path, eng_srt_path), ...]`. The
caller writes into `data/derived/<wrapper>/<stem>/`; this module never
writes to `bt/`.

The SRT listing fed to the model includes cue count + time span +
first-line preview, so forced / SDH / full-dialogue tracks are
distinguishable even when filenames are identical or unhelpful
(`1.srt` / `2.srt`, RARBG's `2_English.srt` + `3_English.srt`).

Bonus-content videos (Featurettes / Extras / Behind-the-Scenes / etc.)
are identified by the model and skipped — we don't burn whisper +
translation + HLS cycles on them. Their existence in `bt/` is
otherwise untouched.

No sentinel file. `bt/` is read-only; this function is cheap (~$0.001
per call) and idempotent, so re-pair on every scan tick.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from annotate import parse_srt
from claude_client import generate_json

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm"}

# Cap prompt size so a hostile or absurdly large layout can't blow the
# model's context. 200 entries covers a TV box-set season pack with
# multilingual Subs/ subfolders comfortably.
MAX_TREE_ENTRIES = 200

_MODEL = os.environ.get("ANTHROPIC_FILTER_MODEL", "claude-haiku-4-5-20251001")


@dataclass(frozen=True)
class Pairing:
    stem: str                    # video filename without extension
    video_path: Path             # absolute path to the source video in bt/
    eng_srt_path: Path | None    # absolute path to bundled English SRT, if any


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
                "description": "relative path of a directory whose video contents are bonus material (featurettes / behind-the-scenes / interviews / extras / trailers), not the main feature; videos inside will be skipped by the pipeline",
            },
        },
    },
    "required": ["srt_matches", "bonus_dirs"],
}


_PROMPT_TEMPLATE = """\
A freshly-downloaded BT torrent. Two decisions:

A) For each MAIN-FEATURE video, pick the best English subtitle to \
attach. Omit a video from srt_matches if no usable English SRT exists.

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
feature — the pipeline will skip those videos entirely. List their \
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
    parseable as SRT."""
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


def pair_wrapper(wrapper: Path) -> list[Pairing]:
    """One LLM call to pair main-feature videos with English SRTs.

    Pure: never writes to `wrapper` or anywhere else. Returns the list of
    Pairings the caller should dispatch into `data/derived/`.
    """
    if not wrapper.is_dir():
        return []

    short = wrapper.name[:40]
    tree = _build_tree(wrapper)
    if not tree:
        return []

    prompt = _PROMPT_TEMPLATE.format(wrapper_name=wrapper.name, tree="\n".join(tree))
    try:
        result = generate_json(prompt, _SCHEMA, model=_MODEL, temperature=0.0)
    except Exception as e:
        print(f"[pair {short}] LLM call failed ({e})", flush=True)
        return []

    # Bonus directories: videos under these are skipped (not paired).
    bonus_dirs: set[Path] = set()
    for rel in result.get("bonus_dirs", []):
        p = _safe_resolve(wrapper, rel)
        if p is None or not p.is_dir():
            print(f"[pair {short}] bonus dir bad path: {rel!r}", flush=True)
            continue
        if p.resolve() == wrapper.resolve():
            print(f"[pair {short}] bonus dir is wrapper root, ignoring", flush=True)
            continue
        bonus_dirs.add(p.resolve())

    def _under_bonus(video: Path) -> bool:
        video_r = video.resolve()
        for bd in bonus_dirs:
            try:
                video_r.relative_to(bd)
                return True
            except ValueError:
                continue
        return False

    all_videos = [p for p in wrapper.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]

    # Defense: if EVERY video falls inside a flagged bonus_dir, the LLM
    # almost certainly mis-classified. Better to over-process than to
    # produce an empty pairing list and silently process nothing.
    main_videos = [v for v in all_videos if not _under_bonus(v)]
    if not main_videos and all_videos:
        print(f"[pair {short}] every video falls in a bonus_dir — ignoring bonus_dirs (LLM likely wrong)", flush=True)
        main_videos = all_videos

    # Build SRT-by-video-name map from LLM matches. Index by filename
    # (not full path) because we use the video's basename as the stem
    # downstream regardless of nesting.
    srt_by_video_name: dict[str, Path] = {}
    for m in result.get("srt_matches", []):
        video_rel = m.get("video", "")
        srt_rel = m.get("srt", "")
        video_p = _safe_resolve(wrapper, video_rel)
        srt_p = _safe_resolve(wrapper, srt_rel)
        if video_p is None or srt_p is None:
            print(f"[pair {short}] bad match path: {m}", flush=True)
            continue
        if video_p.suffix.lower() not in VIDEO_EXTS:
            print(f"[pair {short}] not a video: {video_p.name}", flush=True)
            continue
        if srt_p.suffix.lower() != ".srt":
            print(f"[pair {short}] not an srt: {srt_p.name}", flush=True)
            continue
        if not video_p.is_file() or not srt_p.is_file():
            continue
        srt_by_video_name[video_p.name] = srt_p

    # Final list of pairings. Stems must be unique within a wrapper because
    # they key the `derived/<wrapper>/<stem>/` directory; warn + keep first
    # on collision (effectively rare — two videos with identical basename
    # in one release would be a packaging mistake).
    pairings: list[Pairing] = []
    seen_stems: set[str] = set()
    for video in main_videos:
        if video.stem in seen_stems:
            print(f"[pair {short}] stem collision: {video.stem} (skipping {video.relative_to(wrapper)})", flush=True)
            continue
        seen_stems.add(video.stem)
        pairings.append(Pairing(
            stem=video.stem,
            video_path=video,
            eng_srt_path=srt_by_video_name.get(video.name),
        ))
    return pairings
