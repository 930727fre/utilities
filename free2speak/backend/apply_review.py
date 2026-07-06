"""Materialize a Claude-user discussion into the error book.

Used with discuss-mode uploads (`/upload?auto_analyze=false`). The upload
lands a session with just a transcript; the actual additions/graduations
get decided async in a Claude conversation. Once decided, this script
writes them to disk in the exact same file format as tinder-swipe would
have produced — so drill/roleplay downstream doesn't know the difference.

Usage inside the free2speak-backend container:

    cat review.json | docker exec -i free2speak-backend \
        python /app/apply_review.py <session_id>

review.json:

    {
      "additions": [
        {
          "title": "後預約 → appointment 誤用",
          "you_said": "I have a two \"reservation\" at 3 p.m.",
          "native": "I have two \"appointments\" at 3 p.m.",
          "register": "medical / dental scheduling",
          "l1_diagnosis": "中文『預約』一詞蓋所有情境；英文醫療專用 appointment。",
          "note": ""
        },
        ...
      ],
      "graduations": [33, 45]
    }

Errors created here get `source_candidate_id: null` — that distinguishes
manual-discussion cards from tinder-swipe cards during audit. Both get
`source_session_id: <sid>` so both can trace back to the transcript.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import storage  # noqa: E402

TZ = ZoneInfo("Asia/Taipei")


def _render_body(a: dict) -> str:
    """Same shape as main.py:decide when it materializes an addition —
    keeps error-book bodies consistent regardless of origin."""
    parts = [
        f"**you_said**: {a.get('you_said', '')}",
        f"**native**: {a.get('native', '')}",
    ]
    if a.get("register"):
        parts.append(f"**register**: {a['register']}")
    if a.get("l1_diagnosis"):
        parts.append(f"**l1_diagnosis**: {a['l1_diagnosis']}")
    if a.get("note"):
        parts.append(f"**note**: {a['note']}")
    return "\n\n".join(parts)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: apply_review.py <session_id>", file=sys.stderr)
        sys.exit(1)
    sid = sys.argv[1]

    sess = storage.get_session(sid)
    if sess is None:
        print(f"session {sid} not found", file=sys.stderr)
        sys.exit(1)

    try:
        review = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(f"bad review json on stdin: {e}", file=sys.stderr)
        sys.exit(1)

    additions = review.get("additions", []) or []
    graduations = review.get("graduations", []) or []
    today_iso = datetime.now(TZ).date().isoformat()

    added = []
    for a in additions:
        err_id = storage.add_error(
            title=a.get("title", "(untitled)"),
            body_md=_render_body(a),
            source_session_id=sid,
            source_candidate_id=None,  # manual origin — audit trail marker
            today_iso=today_iso,
        )
        added.append((err_id, a.get("title", "")))
        print(f"[apply] added error #{err_id:04d}: {a.get('title', '')}", flush=True)

    graduated = []
    for gid in graduations:
        gid_int = int(gid)
        if storage.graduate_error(gid_int):
            graduated.append(gid_int)
            print(f"[apply] graduated error #{gid_int:04d}", flush=True)
        else:
            print(f"[apply] WARN: could not graduate #{gid_int} "
                  f"(not in active/ — already graduated or wrong id?)",
                  file=sys.stderr, flush=True)

    print(f"\n[apply] summary: {len(added)} added, {len(graduated)} graduated")


if __name__ == "__main__":
    main()
