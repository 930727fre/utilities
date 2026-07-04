"""One-shot: rename existing session files from opaque UUID hex to
`YYYY-MM-DDTHH-MM-SS-<8hex>` format so `ls data/sessions/` is chronologically
sortable and each id encodes its timestamp.

Runs inside the free2speak-backend container:

    docker exec free2speak-backend python /app/rename_sessions.py

What it does:
  1. Scan `data/sessions/*.json` — those are the metadata anchors.
  2. For each: parse uploaded_at → compute new sid → rename the 3 companion
     files (audio, .json, .decisions.jsonl) atomically.
  3. Rewrite `id` field inside each session JSON to the new sid.
  4. Scan every `data/errors/{active,graduated}/*.md` and update any
     `source_session_id:` in front-matter that refers to a renamed sid.
  5. Print old→new map for verification.

Idempotent-ish: files already in new format (starts with 4-digit-year) are
skipped. Errors whose source_session_id doesn't match any known sid are
left alone (they may have been imported from 1.0-archive with sid=None).

Safe to re-run.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import storage  # noqa: E402
import yaml  # noqa: E402

# Recognizes both new-style (`2026-07-05T00-33-30-abcd1234`) and old-style
# (`0ce87f9d7db54743b66f52c6fe5d76c9`) filename stems.
NEW_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{8}$")
OLD_STEM_RE = re.compile(r"^[0-9a-f]{32}$")


def compute_new_sid(old_sid: str, uploaded_at: str) -> str:
    """`uploaded_at` is ISO 8601 (with offset). Turn it into
    `YYYY-MM-DDTHH-MM-SS` (colons → hyphens for URL/filename safety) plus
    first 8 chars of the old UUID for uniqueness. If uploaded_at is empty or
    unparseable, fall back to file mtime."""
    try:
        # Strip fractional and timezone, keep bare local timestamp.
        dt = datetime.fromisoformat(uploaded_at)
        stamp = dt.strftime("%Y-%m-%dT%H-%M-%S")
    except (ValueError, TypeError):
        return old_sid  # can't rename safely; skip
    return f"{stamp}-{old_sid[:8]}"


def rename_sessions() -> dict[str, str]:
    """Do the physical renames, return {old_sid: new_sid}."""
    remap: dict[str, str] = {}
    for meta_path in sorted(storage.SESSIONS.glob("*.json")):
        old_sid = meta_path.stem
        if NEW_STEM_RE.match(old_sid):
            continue  # already renamed
        if not OLD_STEM_RE.match(old_sid):
            print(f"[rename] SKIP {old_sid}: not a recognized session id shape",
                  flush=True)
            continue

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        new_sid = compute_new_sid(old_sid, meta.get("uploaded_at", ""))
        if new_sid == old_sid:
            print(f"[rename] SKIP {old_sid}: uploaded_at unparseable", flush=True)
            continue

        # Rename companions: <old>.mpeg, <old>.decisions.jsonl, plus this .json.
        for src in storage.SESSIONS.glob(f"{old_sid}.*"):
            suffix = src.name[len(old_sid):]  # keeps `.json`, `.decisions.jsonl`, `.mpeg`
            dst = storage.SESSIONS / f"{new_sid}{suffix}"
            src.rename(dst)

        # Rewrite id inside the metadata json.
        new_meta_path = storage.SESSIONS / f"{new_sid}.json"
        meta["id"] = new_sid
        new_meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        remap[old_sid] = new_sid
        print(f"[rename] {old_sid} → {new_sid}", flush=True)
    return remap


def update_error_references(remap: dict[str, str]) -> int:
    """Any error card whose front-matter `source_session_id` points at a
    renamed sid gets updated in place. Returns count of updated files."""
    if not remap:
        return 0
    updated = 0
    for sub in ("active", "graduated"):
        for f in (storage.ERRORS / sub).glob("[0-9]*-*.md"):
            text = f.read_text(encoding="utf-8")
            meta, body = storage._read_frontmatter(text)
            sref = meta.get("source_session_id")
            if sref in remap:
                meta["source_session_id"] = remap[sref]
                f.write_text(
                    storage._write_frontmatter(meta, body), encoding="utf-8")
                updated += 1
                print(f"[rename] error {f.name}: source_session_id updated",
                      flush=True)
    return updated


def main() -> None:
    print("[rename] scanning data/sessions/...")
    remap = rename_sessions()
    print(f"[rename] {len(remap)} session(s) renamed")

    print("[rename] scanning data/errors/ for stale source_session_id refs...")
    n = update_error_references(remap)
    print(f"[rename] {n} error card(s) updated")

    print("[rename] done.")


if __name__ == "__main__":
    main()
