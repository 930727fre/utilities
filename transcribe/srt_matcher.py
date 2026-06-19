"""LLM-assisted SRT-to-video matching.

Two strategies, cheapest first:

1. **Same-folder match** (`_match_in_same_folder`): the original strict-same-
   stem misses bundled SRTs with language codes or release-group tags.
   Ask Haiku to pick from a flat directory listing. Covers the common case of
   `Show.S01E01.en.srt` next to `Show.S01E01.mkv`.

2. **Tool-use agent walk** (`_find_via_agent`): when (1) misses, give Haiku
   `list_dir` + `read_lines` tools so it can browse subfolders (e.g. RARBG's
   `Subs/<episode-stem>/<n>_English.srt` layout, where the official subtitle
   is buried in a nested per-episode folder we don't otherwise scan). Reads
   a few lines to verify the candidate is English dialogue (not SDH, not
   forced-signs, not the wrong language). Sandboxed to the video's parent
   directory, capped at 12 tool calls.

`find_matching_srt(video)` runs (1) then (2). Caller copies (not moves) the
returned SRT to the canonical `<video-stem>.srt` location so the torrent's
own layout stays intact — important for releases that ship multiple language
tracks in the same Subs/ folder.
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

# Haiku is plenty smart for stem-matching + tool-use folder browsing, and an
# order of magnitude cheaper than Sonnet. Override via ANTHROPIC_MATCH_MODEL.
_MATCH_MODEL = os.environ.get("ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5-20251001")

# Agent caps — keep cost + latency bounded.
_MAX_TOOL_CALLS = 12          # absolute step budget per video
_MAX_READ_LINES = 80          # max lines returned per read_lines call
_MAX_READ_CHARS = 6000        # belt-and-suspenders for very long lines


# ── Strategy 1: same-folder flat match ─────────────────────────────────────

_FLAT_SCHEMA = {
    "type": "object",
    "properties": {"match": {"type": ["string", "null"]}},
    "required": ["match"],
}

_FLAT_PROMPT_TEMPLATE = """\
You match a video file to its subtitle (.srt) sidecar in the same folder.

Video filename:
{video_name}

Candidate .srt files in the same folder:
{srt_list}

If one of the .srt files is the subtitle track for this video — same show + \
episode (or same movie title), ignoring differences in language code suffix \
(`.en.srt`, `.eng.srt`), release-group tags, resolution tags, version numbers, \
or whitespace/punctuation variations — respond with that filename verbatim.

If none of them corresponds, respond with null. Be strict: only match when \
you're confident it's the same video.

Output JSON: {{"match": "<exact filename from the list>" | null}}
"""


def _match_in_same_folder(video: Path) -> Optional[Path]:
    from claude_client import generate_json

    folder = video.parent
    try:
        siblings = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".srt"
        )
    except OSError:
        return None
    if not siblings:
        return None

    sibling_names = [s.name for s in siblings]
    prompt = _FLAT_PROMPT_TEMPLATE.format(
        video_name=video.name,
        srt_list="\n".join(f"- {n}" for n in sibling_names),
    )

    try:
        result = generate_json(
            prompt, _FLAT_SCHEMA,
            model=_MATCH_MODEL,
            temperature=0.0,
            max_tokens=200,
        )
    except Exception as e:
        print(f"[srt-matcher/flat] API error for {video.name!r}: {e}", flush=True)
        return None

    matched_name = result.get("match")
    if not matched_name:
        return None
    # Anti-hallucination: must be a real entry in the listing we showed it.
    if matched_name not in sibling_names:
        print(f"[srt-matcher/flat] LLM returned {matched_name!r}, not in folder; ignoring", flush=True)
        return None
    return folder / matched_name


# ── Strategy 2: tool-use agent walk ────────────────────────────────────────

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
- Flat: `<stem>.srt` next to the video → already handled before you were called, so don't expect this.
- RARBG / scene packs: `Subs/<video-stem>/N_English.srt`, possibly alongside `N_English-SDH.srt`, `N_Spanish.srt`, etc.
- Some packs: `subs/eng.srt`, `Subtitles/<EpisodeName>.srt`, single language only.

Strategy:
1. `list_dir(".")` to see what's around — look for a Subs / subs / Subtitles \
folder or stray .srt files.
2. Drill into the most likely subfolder. For multi-episode packs, find the \
subfolder whose name matches THIS video's stem (S01E03 → folder ending in S01E03).
3. When you've found candidate .srt files, `read_lines` on the top 1-2 to \
verify the dialogue is English (real English sentences, not Spanish / Chinese / \
empty / only `[MUSIC]` tags).
4. Prefer clean English dialogue over SDH (deaf/hard-of-hearing — extra \
`[MUSIC]` / `[DOOR SLAMS]` cues mixed in) or Forced (only foreign-language \
signs translated, mostly empty otherwise). SDH is acceptable as a fallback.
5. Call `respond` with the relative path of your pick (e.g. \
`Subs/Show.S01E03.RELEASE/2_English.srt`), or null if nothing usable exists.

Be efficient — you have a small step budget (~10 tool calls). List once, peek \
into the most likely candidates, decide.
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


def _find_via_agent(video: Path) -> Optional[Path]:
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
            print(f"[srt-matcher/agent] API error step {step}: {e}", flush=True)
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
            else:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tid,
                    "content": f"unknown tool: {name}",
                })

        if respond_call is not None:
            match_rel = respond_call.get("match")
            if not match_rel:
                print(f"[srt-matcher/agent] no English sub found for {video.name!r}", flush=True)
                return None
            target = _resolve_in_sandbox(sandbox, match_rel)
            if target is None or not target.is_file() or target.suffix.lower() != ".srt":
                print(f"[srt-matcher/agent] invalid match returned: {match_rel!r}", flush=True)
                return None
            print(f"[srt-matcher/agent] picked {match_rel!r} for {video.name!r}", flush=True)
            return target

        if not tool_results:
            # Model returned text-only (no tool_use) — protocol violation; bail.
            print(f"[srt-matcher/agent] no tool calls at step {step} for {video.name!r}", flush=True)
            return None
        messages.append({"role": "user", "content": tool_results})

    print(f"[srt-matcher/agent] hit step budget for {video.name!r}", flush=True)
    return None


# ── Entry point ───────────────────────────────────────────────────────────

def find_matching_srt(video: Path) -> Optional[tuple[Path, str]]:
    """Find an English subtitle for `video` via LLM. Returns `(source_path,
    stage_tag)` on success or None on miss. `stage_tag` is "bundled-flat"
    when stage 1 picked it (same-folder Haiku) or "bundled-agent" when
    stage 2 (tool-use folder walk) did — the caller passes this to
    `stamp_source` so the SRT records WHICH path produced it, matching
    the `※ source: whisper / opensubtitles-hash / opensubtitles-text`
    convention used elsewhere in the pipeline.

    Caller should COPY (not move) the returned file to `<video-stem>.srt`
    so the original layout (especially nested torrent Subs/ folders)
    stays intact for fallback / other language tracks.

    Two-stage strategy:
      1. Same-folder flat match (cheap, single Haiku call).
      2. Tool-use folder walk (expensive, ~5-12 Haiku calls + tools).
    """
    matched = _match_in_same_folder(video)
    if matched is not None:
        return matched, "bundled-flat"
    matched = _find_via_agent(video)
    if matched is not None:
        return matched, "bundled-agent"
    return None
