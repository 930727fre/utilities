"""Opus drill-generation prompt.

Adapted from `1.0-archive/prompts/drill-generation.md`. 1.0 produced freeform
markdown with collapsible answers; 2.0 stores each card as a row in
`drill_cards` so the frontend can render them as Tinder-swipe flashcards.
"""

# Opus tool-use schema. Returns a single drill set (rationale + N cards).
TOOL = {
    "name": "emit_drill",
    "description": "Emit a generated daily drill set for the user.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {
                "type": "string",
                "description": "One Chinese line explaining what this drill set targets (which errors / patterns).",
            },
            "cards": {
                "type": "array",
                "description": "10 drill cards. ~7 sourced from active errors, ~3 from recent session content. Mix of fill_blank (~6-7) and translate (~3-4).",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["translate", "fill_blank"],
                            "description": "translate = Chinese sentence to translate into English. fill_blank = English sentence with key word(s) blanked out, plus a Chinese hint.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "What the user sees. For translate: Chinese sentence. For fill_blank: Chinese hint + English sentence with ___ for the blank.",
                        },
                        "answer": {
                            "type": "string",
                            "description": "Full English answer. For fill_blank, the previously-blanked word(s) should be **bold** in the answer.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "1 short line — which error / pattern this card targets (Chinese OK).",
                        },
                        "source_error_id": {
                            "type": ["integer", "null"],
                            "description": "DB id of the source active error if the card targets one; null if it's from session content.",
                        },
                    },
                    "required": ["kind", "prompt", "answer", "rationale"],
                },
            },
        },
        "required": ["rationale", "cards"],
    },
}


def build(active_errors: list, recent_sessions: list) -> str:
    """Render the drill-generation prompt.

    Args:
      active_errors: rows from `errors` where status='active' — primary source.
      recent_sessions: last ~5 sessions (id, transcript, summary) — secondary source for fresh phrasings.
    """
    parts = [
        "You generate a daily 10-card drill set for a Chinese-L1 English learner (Taiwan, upper-intermediate).",
        "Each card is one Tinder-swipe item: the user reads the prompt, says their answer aloud, then flips to see the canonical answer.",
        "",
        "## Mix",
        "- ~7 cards from active errors (recurring / high-frequency / recent priority).",
        "- ~3 cards from recent session content — fresh phrasings, idiomatic expressions, native AI lines worth recycling. NOT verbatim copies of error examples.",
        "- Card types: ~6-7 fill_blank + ~3-4 translate. Fill_blank preserves context so recall is precise.",
        "",
        "## Card design rules",
        "- Each card targets ONE pattern. Don't combine multiple errors into one prompt.",
        "- New sentences — do NOT reuse the example sentences from the error book verbatim. Same pattern, different example.",
        "- fill_blank: blank out the key word(s) that test the pattern. Show enough context that the gap is unambiguous given the Chinese hint.",
        "  Example fill_blank prompt: 'Chinese: 我習慣每天早上跑步。 / English: I ___ ___ going for a run every morning.'",
        "  Example fill_blank answer: 'I **am used to** going for a run every morning.'",
        "- translate: Chinese sentence → user produces full English. Use this when the pattern is too freeform for fill_blank.",
        "  Example translate prompt: '把這句翻成英文：「他們會再回覆我關於押金的事。」'",
        "  Example translate answer: \"They'll get back to me about the deposit.\"",
        "",
        "## Output",
        "Invoke the `emit_drill` tool. No prose around it.",
        "",
    ]

    if active_errors:
        parts.append("Active errors (use 7-ish of these — pick high-frequency / recent / recurring):")
        for e in active_errors[:30]:
            body = (e["body_md"] or "")[:200].replace("\n", " ")
            parts.append(f"- id={e['id']} · {e['title']} · {body}")
        parts.append("")

    if recent_sessions:
        parts.append("Recent session content (use ~3 cards from fresh phrasings here, NOT from active errors):")
        for s in recent_sessions[:5]:
            transcript = (s.get("transcript") or "").replace("\n", " ")[:600]
            parts.append(f"- {s.get('uploaded_at', '')}: {transcript}")
        parts.append("")

    if not active_errors and not recent_sessions:
        parts.append("Cold start: no errors and no sessions yet. Skip drill generation by returning an empty cards array with rationale='cold start — no material'.")

    return "\n".join(parts)
