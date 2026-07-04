"""Re-run Claude analysis on an existing session's stored transcript.

Useful for A/B-testing prompt tweaks without re-uploading the audio (skips the
Gemini transcribe pass — same transcript in, same variable-control conditions,
only the prompt changes).

Usage inside the free2speak-backend container:

    docker exec free2speak-backend python /app/reanalyze.py <session_id>

Overwrites the session's `raw_response` field with the fresh analysis. Wipes
`<sid>.decisions.jsonl` to zero because candidate IDs (`add-1`, `add-2`, ...)
may point to different content now — old swipes would misalign.

Uses the shared `analyzer.analyze()` so it inherits the retry + Opus fallback
that main.py's /upload uses.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import storage  # noqa: E402
from analyzer import analyze  # noqa: E402

ACTIVE_ERROR_LIMIT = 100


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python reanalyze.py <session_id>", file=sys.stderr)
        sys.exit(1)
    sid = sys.argv[1]

    sess = storage.get_session(sid)
    if sess is None:
        print(f"session {sid} not found", file=sys.stderr)
        sys.exit(1)

    transcript = sess.get("transcript") or sess.get("raw_response", {}).get("transcript", "")
    if not transcript:
        print(f"session {sid} has no transcript to re-analyze", file=sys.stderr)
        sys.exit(1)

    print(f"[reanalyze] {sid}")
    print(f"[reanalyze] transcript: {len(transcript)} chars")

    active_errors = storage.list_active_errors(limit=ACTIVE_ERROR_LIMIT)
    analysis = analyze(transcript, active_errors)
    analysis["transcript"] = transcript

    print(f"[reanalyze] got {len(analysis.get('additions', []))} additions, "
          f"{len(analysis.get('graduations', []))} graduations")

    sess["raw_response"] = analysis
    sess["summary"] = analysis.get("summary", "")
    sess["fluency_notes"] = analysis.get("fluency_notes", "")
    storage._session_meta_path(sid).write_text(
        json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")

    decisions_path = storage._session_decisions_path(sid)
    if decisions_path.exists():
        decisions_path.write_text("", encoding="utf-8")
        print("[reanalyze] wiped decisions.jsonl")

    print("[reanalyze] done.")


if __name__ == "__main__":
    main()
