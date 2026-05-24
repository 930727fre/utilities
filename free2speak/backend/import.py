"""One-shot import of 1.0 md/json data into the 2.0 SQLite DB.

Reads from /data/{errors.md, roleplays/, sessions/, drills/}, writes to
/data/free2speak.db. Refuses to run if the DB already has data — delete
the DB file to re-run.

Run inside the backend container:
    docker exec free2speak-backend python /app/import.py
"""

import json
import re
import sys
from pathlib import Path

from db import DB_PATH, connect, init_schema

DATA = Path("/data")


# ─── errors.md ────────────────────────────────────────────────────────────────

def parse_errors_md() -> list[dict]:
    path = DATA / "errors.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")

    # Split on `### ` (entry headings). First chunk is the file header.
    chunks = re.split(r"^### ", text, flags=re.MULTILINE)[1:]

    out = []
    for chunk in chunks:
        lines = chunk.rstrip().split("\n")
        title = lines[0].strip()
        body_lines: list[str] = []
        last_seen: str | None = None
        for line in lines[1:]:
            m = re.match(r"-\s*上次出現[：:]\s*(\S+)", line)
            if m:
                last_seen = m.group(1)
            else:
                body_lines.append(line)
        body_md = "\n".join(body_lines).strip()
        if title:
            out.append({"title": title, "body_md": body_md, "last_seen": last_seen})
    return out


# ─── roleplays/*.md ───────────────────────────────────────────────────────────

def parse_roleplay_md(path: Path) -> dict:
    stem = path.stem  # "YYYY-MM-DD-topic"
    date = stem[:10]
    topic = stem[11:] if len(stem) > 11 else ""
    text = path.read_text(encoding="utf-8")

    # Rationale: leading "> ... rationale: X" block (one or more `> ` lines).
    # Pull contiguous `> ` lines, strip the optional "rationale:" prefix.
    quote_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith(">"):
            quote_lines.append(line.lstrip("> ").rstrip())
        elif quote_lines:
            break  # end of the leading quote block
    rationale = " ".join(quote_lines).strip()
    rationale = re.sub(r"^(?:今日選題|選題)?\s*rationale\s*[：:]\s*", "", rationale)

    # Body: everything after the H1 line.
    lines = text.split("\n")
    body_start = 1
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    body_md = "\n".join(lines[body_start:]).strip()

    return {
        "id": stem,
        "date": date,
        "topic": topic,
        "rationale": rationale,
        "body_md": body_md,
    }


# ─── sessions/*.json ──────────────────────────────────────────────────────────

def parse_session_json(path: Path) -> dict:
    stem = path.stem
    date = stem[:10]
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "id": stem,
        "roleplay_id": stem,  # 1.0 convention: 1:1 match by stem
        "transcript": data.get("transcript", ""),
        "summary": data.get("summary", ""),
        "fluency_notes": data.get("fluency_notes", ""),
        "raw_response": json.dumps(data, ensure_ascii=False),
        "uploaded_at": f"{date} 12:00:00",
    }


# ─── drills/*.md ──────────────────────────────────────────────────────────────

def parse_drill_md(path: Path) -> tuple[dict, list[dict]]:
    stem = path.stem  # "YYYY-MM-DD"
    text = path.read_text(encoding="utf-8")

    # Rationale: first contiguous `> ` block under the H1.
    quote_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith(">"):
            quote_lines.append(line.lstrip("> ").rstrip())
        elif quote_lines:
            break
    rationale = " ".join(quote_lines).strip()

    # First-pass section only — second pass is just reordering.
    first_pass = re.search(
        r"## 第一遍.*?(?=^## 第二遍|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    cards: list[dict] = []
    if first_pass:
        parts = re.split(r"\n### (\d+)\. \[(.+?)\]", first_pass.group(0))
        # parts = [pre, n1, type1, body1, n2, type2, body2, ...]
        for i in range(1, len(parts), 3):
            order_index = int(parts[i])
            kind_label = parts[i + 1]
            body = parts[i + 2]
            kind = "translate" if "中翻英" in kind_label else "fill_blank"

            prompt_part, _, details_part = body.partition("<details>")
            prompt = prompt_part.strip()

            ans = re.search(
                r"\*\*(?:英文|完整)[：:]\*\*\s*(.+?)(?=\n\*\*針對[：:]|\Z)",
                details_part,
                re.DOTALL,
            )
            answer = ans.group(1).strip() if ans else ""

            rat = re.search(
                r"\*\*針對[：:]\*\*\s*(.+?)(?=</details>|\Z)",
                details_part,
                re.DOTALL,
            )
            card_rationale = rat.group(1).strip() if rat else ""

            cards.append({
                "order_index": order_index,
                "kind": kind,
                "prompt": prompt,
                "answer": answer,
                "rationale": card_rationale,
            })

    drill = {"id": stem, "rationale": rationale, "completed_at": stem}
    return drill, cards


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    conn = connect()
    init_schema(conn)

    # Refuse to import into a non-empty DB.
    for table in ("errors", "roleplays", "sessions", "drills", "drill_cards"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if n > 0:
            print(f"ERROR: {table} already has {n} row(s). Refusing to import.")
            print(f"To re-run, delete the DB first:\n  rm {DB_PATH}")
            return 1

    err_count = rp_count = sess_count = drill_count = card_count = 0

    for e in parse_errors_md():
        conn.execute(
            """
            INSERT INTO errors (title, body_md, first_seen_date, last_seen_date, status)
            VALUES (?, ?, ?, ?, 'active')
            """,
            (e["title"], e["body_md"], e["last_seen"], e["last_seen"]),
        )
        err_count += 1

    rp_dir = DATA / "roleplays"
    if rp_dir.exists():
        for f in sorted(rp_dir.glob("*.md")):
            if f.name == "index.md":
                continue
            rp = parse_roleplay_md(f)
            conn.execute(
                """
                INSERT INTO roleplays (id, date, topic, rationale, body_md, status)
                VALUES (?, ?, ?, ?, ?, 'done')
                """,
                (rp["id"], rp["date"], rp["topic"], rp["rationale"], rp["body_md"]),
            )
            rp_count += 1

    sess_dir = DATA / "sessions"
    if sess_dir.exists():
        for f in sorted(sess_dir.glob("*.json")):
            s = parse_session_json(f)
            conn.execute(
                """
                INSERT INTO sessions
                  (id, roleplay_id, transcript, summary, fluency_notes,
                   raw_response, review_done, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    s["id"], s["roleplay_id"], s["transcript"], s["summary"],
                    s["fluency_notes"], s["raw_response"], s["uploaded_at"],
                ),
            )
            sess_count += 1

    drill_dir = DATA / "drills"
    if drill_dir.exists():
        for f in sorted(drill_dir.glob("*.md")):
            if f.name == "index.md":
                continue
            drill, cards = parse_drill_md(f)
            conn.execute(
                "INSERT INTO drills (id, rationale, completed_at) VALUES (?, ?, ?)",
                (drill["id"], drill["rationale"], drill["completed_at"]),
            )
            for c in cards:
                conn.execute(
                    """
                    INSERT INTO drill_cards
                      (drill_id, order_index, kind, prompt, answer, rationale)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        drill["id"], c["order_index"], c["kind"],
                        c["prompt"], c["answer"], c["rationale"],
                    ),
                )
                card_count += 1
            drill_count += 1

    conn.commit()
    conn.close()

    print(
        f"Imported:\n"
        f"  {err_count} errors\n"
        f"  {rp_count} roleplays\n"
        f"  {sess_count} sessions\n"
        f"  {drill_count} drills ({card_count} cards)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
