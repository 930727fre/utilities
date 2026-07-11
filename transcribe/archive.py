"""Auto-archive canonical SRTs + auto-attach on re-download.

Every canonical SRT the pipeline produces gets mirrored to
`data/archive/<title>/…` under a flat show-title tree (Movies/ and TV/
prefixes stripped — the tree groups by title, not media kind). When
a new BT wrapper enters `process_bt_file`, an `"archive"` candidate
tier looks up the archive by Gemini-decided title match + strict SxxExx
episode key and short-circuits whisper re-work + LLM re-annotation.

Title match is delegated to Gemini Flash Lite (`gemini_client.generate_json`).
One API call per BT-video processed, negligible cost (~$0.0005), handles
edge cases pure regex would miss (translated titles, subtitle format
rewrites like `Star Wars: A New Hope` ↔ `Star Wars Episode IV`,
punctuation drift, etc.). Regex loose-match was the initial choice but
Gemini gives better recall for the same essentially-free price.

Episode key match stays regex (`SxxExx` → tuple) — deterministic and
Gemini gains nothing there.

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
from gemini_client import generate_json

ARCHIVE_ROOT = Path(os.environ.get("SRT_ARCHIVE_ROOT", "/app/data/archive"))
ENABLED = os.environ.get("SRT_ARCHIVE_ENABLED", "true").lower() != "false"
# `gemini_client.py`'s DEFAULT_MODEL points at a name the API doesn't currently
# accept, so we pin explicitly. Cheap-enough tier for a one-shot classifier.
_MATCH_MODEL = os.environ.get("ARCHIVE_MATCH_MODEL", "gemini-2.5-flash-lite")

_EP_RE = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")
_ZH_SUFFIX = ".zh-tw.srt"


# ── episode key ──────────────────────────────────────────────────────────

def _episode_key(stem: str) -> Optional[tuple[int, int]]:
    """Extract (season, episode) tuple from a filename stem, or None if the
    stem isn't a TV episode. Deterministic, no LLM needed."""
    m = _EP_RE.search(stem)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ── title match via Gemini ──────────────────────────────────────────────

_MATCH_PROMPT = """\
You match media library titles. Given a "target" folder name (a show or movie \
title) and a list of existing archive folder names, decide which archive folder \
(if any) refers to the SAME work as the target.

Accept as same-work (return match):
- Punctuation differences (Ocean's Eleven ↔ Oceans Eleven)
- Article prefix presence (The Wire ↔ Wire)
- Year presence differences (Chernobyl ↔ Chernobyl (2019))
- Non-year parenthetical qualifiers (Chernobyl (2019) ↔ Chernobyl (Mini-Series))
- Subtitle format rewrites (Star Wars: A New Hope ↔ Star Wars Episode IV)
- Translation between Chinese and English if the year matches
- Abbreviation ↔ full title if unambiguous (GoT ↔ Game of Thrones)

Reject as different-work (return null):
- Same title but different year → remake, different work (Titanic (1997) ↔ Titanic (2023))
- Completely unrelated titles that happen to share a word (Fringe ↔ Cutting Edge)
- Different works in the same franchise (Star Wars: A New Hope ↔ Star Wars: The Force Awakens)

Target:
{target}

Archive folders:
{options}

Return JSON: {{"match": "<exact archive folder name from the list above>"}} if you \
find a same-work match, otherwise {{"match": ""}} (empty string).

The match value MUST be one of the listed archive folder names verbatim, or empty. \
Do not invent a folder name that isn't in the list.
"""

# Gemini's responseSchema doesn't support union types (`"type": ["string", "null"]`);
# use empty string as the no-match sentinel instead.
_MATCH_SCHEMA = {
    "type": "object",
    "properties": {"match": {"type": "string"}},
    "required": ["match"],
}


def _match_archive_folder(canonical_show: str, archive_dirs: list[Path]) -> Optional[Path]:
    """Ask Gemini which archive folder (if any) is the same work as
    canonical_show. Returns the matched Path, or None if no match / API
    fails / model hallucinates a non-listed folder name."""
    if not archive_dirs:
        return None
    options = "\n".join(f"- {d.name}" for d in archive_dirs)
    prompt = _MATCH_PROMPT.format(target=canonical_show, options=options)
    try:
        result = generate_json(prompt, _MATCH_SCHEMA, temperature=0.0, model=_MATCH_MODEL)
    except Exception as e:
        print(f"[archive] gemini match failed for {canonical_show!r}: {e}", flush=True)
        return None
    matched_name = (result.get("match") or "").strip()
    if not matched_name:
        return None  # empty string sentinel from prompt = no match
    # Guard against a hallucinated name not in the actual list.
    for d in archive_dirs:
        if d.name == matched_name:
            return d
    print(f"[archive] gemini returned unrecognized name {matched_name!r}; treating as no match", flush=True)
    return None


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


# ── lookup on new BT arrival ────────────────────────────────────────────

def find_archive_english(canonical_video: Path) -> Optional[Path]:
    """Return the archived English SRT for this video, or None.

    TV lookup: Gemini picks archive folder from title list + strict SxxExx
      key match at file level. Handles LLM canonical drift and any other
      naming variance ("Chernobyl (2019)" ↔ "Chernobyl (Mini-Series)",
      "The Wire" ↔ "Wire", etc.).
    Movie lookup: Gemini folder pick + single-SRT convention within folder.
    """
    if not ENABLED or not ARCHIVE_ROOT.is_dir():
        return None
    try:
        rel = canonical_video.resolve().relative_to(ARTIFACT_ROOT.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2 or parts[0] not in ("Movies", "TV"):
        return None
    show_title = parts[1]  # e.g. "Chernobyl (2019)"

    archive_dirs = [d for d in ARCHIVE_ROOT.iterdir() if d.is_dir()]
    matched_folder = _match_archive_folder(show_title, archive_dirs)
    if matched_folder is None:
        return None

    if parts[0] == "Movies":
        eng = [
            p for p in matched_folder.rglob("*.srt")
            if not p.name.endswith(_ZH_SUFFIX)
        ]
        # Movies archive folder should hold exactly one English SRT;
        # if a re-download produced multiple (edge case), decline.
        return eng[0] if len(eng) == 1 else None

    # TV
    ep = _episode_key(canonical_video.stem)
    if ep is None:
        return None
    for p in matched_folder.rglob("*.srt"):
        if p.name.endswith(_ZH_SUFFIX):
            continue
        if _episode_key(p.stem) == ep:
            return p
    return None


def find_archive_zh(archive_english_srt: Path) -> Optional[Path]:
    """Given the matched English archive SRT, return the sibling
    <stem>.zh-tw.srt if the archive also has a Chinese translation."""
    zh = archive_english_srt.parent / f"{archive_english_srt.stem}{_ZH_SUFFIX}"
    return zh if zh.exists() else None
