"""LLM-assisted SRT-to-video matching.

Single strategy: a tool-use Haiku agent that browses the video's folder
tree via `list_dir` + `read_lines` + `srt_summary`, then returns the
relative path of its pick. Handles flat-folder bundles
(`Show.S01E01.en.srt` next to `Show.S01E01.mkv`), nested layouts
(RARBG's `Subs/<episode-stem>/N_English.srt`), and anything in between
the same way — the agent just lists what's around and decides.

`srt_summary` is the key disambiguator when multiple English candidates
exist (RARBG packs typically ship 3: forced, full dialogue, SDH).
Returns cue count + first/last timestamp so the agent can tell forced
tracks (few cues, narrow time span — translate only foreign-language
scenes or signs) apart from full dialogue tracks (hundreds of cues
across the episode). Coverage is more reliable than naming convention,
which is not standardised across release groups.

Sandboxed to the video's parent directory (no path escape), capped at 12
tool calls. Caller copies (not moves) the returned SRT to the canonical
`<video-stem>.srt` location so the torrent's own layout stays intact —
important for releases that ship multiple language tracks in the same
Subs/ folder.
"""
import os
from pathlib import Path
from typing import Any, Optional

import requests

from claude_client import (
    API_URL,
    API_VERSION,
    _api_key,
)

# Haiku is plenty smart for this and an order of magnitude cheaper than
# Sonnet. Override via ANTHROPIC_MATCH_MODEL.
_MATCH_MODEL = os.environ.get("ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5-20251001")

# Agent caps — keep cost + latency bounded.
_MAX_TOOL_CALLS = 12          # absolute step budget per video
_MAX_READ_LINES = 80          # max lines returned per read_lines call
_MAX_READ_CHARS = 6000        # belt-and-suspenders for very long lines


_AGENT_TOOLS = [
    {
        "name": "list_dir",
        "description": (
            "List files and subfolders inside a directory under the video's "
            "folder. Returns one entry per line: 'F <name>' for files, "
            "'D <name>' for directories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to the video's folder. Use '.' for the video's own folder.",
                }
            },
            "required": ["relative_path"],
        },
    },
    {
        "name": "read_lines",
        "description": (
            "Read the first lines of a text file (e.g. a .srt candidate) to "
            "verify language and content. Capped at 80 lines / 6000 chars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to the video's folder.",
                }
            },
            "required": ["relative_path"],
        },
    },
    {
        "name": "srt_summary",
        "description": (
            "For an SRT file, return cue count + first/last cue timestamps. "
            "Use this to distinguish forced/fragment tracks (~10 cues "
            "spanning 1-2 minutes — only translate foreign-language scenes "
            "or on-screen text) from full dialogue tracks (hundreds of cues "
            "spanning the whole episode runtime). Coverage is the most "
            "reliable signal regardless of release group naming convention."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to the video's folder.",
                }
            },
            "required": ["relative_path"],
        },
    },
    {
        "name": "respond",
        "description": (
            "Final answer. Provide the relative path of the chosen English "
            "subtitle file, or null if no usable English subtitle exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "match": {
                    "type": ["string", "null"],
                    "description": "Relative path of the chosen .srt file (within the video's folder), or null.",
                }
            },
            "required": ["match"],
        },
    },
]

_AGENT_PROMPT_TEMPLATE = """\
You're looking for the official English subtitle for a video by browsing its \
folder tree. Use the tools to navigate, then call `respond`.

Video filename: {video_name}
Folder root: `.` (the video's own folder — all relative paths are inside it)

Typical layouts you'll see:
- Flat: `<stem>.srt` or `<stem>.en.srt` next to the video.
- RARBG / scene packs: `Subs/<video-stem>/N_English.srt`, possibly alongside \
`N_English-SDH.srt`, `N_Spanish.srt`, etc.
- Some packs: `subs/eng.srt`, `Subtitles/<EpisodeName>.srt`, single language only.

Strategy:
1. `list_dir(".")` to see what's around — look for a Subs / subs / Subtitles \
folder or stray .srt files.
2. If you see candidate .srt files directly in `.`, you can `read_lines` one \
to verify and `respond`. If you only see subfolders, drill in. For multi-episode \
packs, find the subfolder whose name matches THIS video's stem (S01E03 → folder \
ending in S01E03).
3. When you find MULTIPLE candidate .srt files (e.g. RARBG ships three: \
`2_English.srt`, `3_English.srt`, `4_English.srt`), call `srt_summary` on \
each to compare coverage. The naming convention is not reliable — `2_English` \
in one episode is forced, in another might be the full track. What IS reliable:
  - **Forced / fragment tracks**: ~10 cues, time span <2 minutes. \
These translate foreign-language scenes or signs only and will leave most of \
the episode without subs. AVOID unless nothing else exists.
  - **Full dialogue tracks**: hundreds of cues spanning the whole episode \
runtime (e.g. 00:00 → 00:55 for a 55-min episode). PREFER these.
  - **SDH**: also hundreds of cues, similar coverage to full, but contains \
extra `[MUSIC]` / `[DOOR SLAMS]` cues mixed in. Acceptable fallback.
4. After picking by coverage, `read_lines` on it to verify the dialogue is \
actually English (not Spanish / Chinese / empty).
5. Call `respond` with the relative path of your pick (e.g. \
`Subs/Show.S01E03.RELEASE/3_English.srt` or `Show.S01E01.en.srt`), or null \
if nothing usable exists.

Be efficient — you have a small step budget (~10 tool calls). List once, \
summarize candidates, peek for language verification, decide.
"""


def _resolve_in_sandbox(sandbox: Path, rel: str) -> Optional[Path]:
    """Resolve `rel` under `sandbox`; return None if it escapes."""
    try:
        target = (sandbox / rel).resolve()
        target.relative_to(sandbox)
        return target
    except (ValueError, OSError):
        return None


def _tool_list_dir(sandbox: Path, args: dict) -> str:
    rel = args.get("relative_path", ".")
    target = _resolve_in_sandbox(sandbox, rel)
    if target is None:
        return "error: path escapes the video folder"
    if not target.exists():
        return "error: path does not exist"
    if not target.is_dir():
        return "error: not a directory"
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as e:
        return f"error: {e}"
    if not entries:
        return "(empty)"
    return "\n".join(f"{'D' if p.is_dir() else 'F'} {p.name}" for p in entries)


def _tool_read_lines(sandbox: Path, args: dict) -> str:
    rel = args.get("relative_path", "")
    target = _resolve_in_sandbox(sandbox, rel)
    if target is None:
        return "error: path escapes the video folder"
    if not target.exists():
        return "error: path does not exist"
    if not target.is_file():
        return "error: not a file"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"error: {e}"
    if len(text) > _MAX_READ_CHARS:
        text = text[:_MAX_READ_CHARS] + "\n...(truncated)"
    lines = text.split("\n")
    if len(lines) > _MAX_READ_LINES:
        lines = lines[:_MAX_READ_LINES] + ["...(truncated)"]
    return "\n".join(lines)


def _tool_srt_summary(sandbox: Path, args: dict) -> str:
    rel = args.get("relative_path", "")
    target = _resolve_in_sandbox(sandbox, rel)
    if target is None:
        return "error: path escapes the video folder"
    if not target.exists():
        return "error: path does not exist"
    if not target.is_file():
        return "error: not a file"
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as e:
        return f"error: {e}"
    # Local import: parse_srt lives in annotate.py and we already reuse it
    # from translator.py too; importing at call time keeps srt_matcher
    # importable even if annotate's deps go sideways at startup.
    from annotate import parse_srt
    try:
        cues = parse_srt(text)
    except Exception as e:
        return f"error parsing srt: {e}"
    if not cues:
        return "no parseable cues (might be binary subs, .idx/.sub, or malformed)"
    return f"{len(cues)} cues, {cues[0]['time'].split(' --> ')[0]} → {cues[-1]['time'].split(' --> ')[0]}"


def find_matching_srt(video: Path) -> Optional[tuple[Path, str]]:
    """Find an English subtitle for `video` via tool-use Haiku agent.
    Returns `(source_path, "bundled-recursive")` on success or None on miss.

    The "bundled-recursive" stage tag matches the `※ source: …` convention
    used by the rest of the pipeline — caller passes it to `stamp_source`
    after copying the matched SRT into place. "Recursive" distinguishes
    this case (agent had to walk into Subs/ or similar subfolders to find
    the SRT) from "bundled-strict-stem" (a `<stem>.srt` sitting right next
    to the video, found without invoking the agent).

    Caller should COPY (not move) the returned file to `<video-stem>.srt`
    so the original layout (especially nested torrent Subs/ folders)
    stays intact for fallback / other language tracks.
    """
    sandbox = video.parent.resolve()
    prompt = _AGENT_PROMPT_TEMPLATE.format(video_name=video.name)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    for step in range(_MAX_TOOL_CALLS):
        body = {
            "model": _MATCH_MODEL,
            "max_tokens": 2048,
            "temperature": 0.0,
            "messages": messages,
            "tools": _AGENT_TOOLS,
        }
        try:
            r = requests.post(API_URL, json=body, headers=headers, timeout=(10, 60))
            r.raise_for_status()
            resp = r.json()
        except requests.RequestException as e:
            print(f"[srt-matcher] API error step {step}: {e}", flush=True)
            return None

        # Append the assistant turn verbatim so subsequent tool_result blocks
        # can reference its tool_use ids.
        assistant_content = resp.get("content") or []
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results: list[dict] = []
        respond_call: Optional[dict] = None
        for block in assistant_content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input") or {}
            tid = block.get("id")
            if name == "respond":
                respond_call = inp
                break
            elif name == "list_dir":
                out = _tool_list_dir(sandbox, inp)
                tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": out})
            elif name == "read_lines":
                out = _tool_read_lines(sandbox, inp)
                tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": out})
            elif name == "srt_summary":
                out = _tool_srt_summary(sandbox, inp)
                tool_results.append({"type": "tool_result", "tool_use_id": tid, "content": out})
            else:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tid,
                    "content": f"unknown tool: {name}",
                })

        if respond_call is not None:
            match_rel = respond_call.get("match")
            if not match_rel:
                print(f"[srt-matcher] no English sub found for {video.name!r}", flush=True)
                return None
            target = _resolve_in_sandbox(sandbox, match_rel)
            if target is None or not target.is_file() or target.suffix.lower() != ".srt":
                print(f"[srt-matcher] invalid match returned: {match_rel!r}", flush=True)
                return None
            print(f"[srt-matcher] picked {match_rel!r} for {video.name!r}", flush=True)
            return target, "bundled-recursive"

        if not tool_results:
            # Model returned text-only (no tool_use) — protocol violation; bail.
            print(f"[srt-matcher] no tool calls at step {step} for {video.name!r}", flush=True)
            return None
        messages.append({"role": "user", "content": tool_results})

    print(f"[srt-matcher] hit step budget for {video.name!r}", flush=True)
    return None
