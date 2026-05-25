"""Gemini audio-analysis prompt + structured-output schema.

Adapted from `1.0-archive/prompts/gemini-analysis.md` and extended for 2.0:
- detect graduations against active errors (by their DB ids)
- group recurring patterns into a single addition (one card per pattern)
- wrap the specific error/correction portion in "" so the frontend can highlight it
- skip self-corrections and other nitpicks
- Chinese-L1-aware: prompt explicitly mentions common Mandarin transfer patterns
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "summary": {"type": "string"},
        "fluency_notes": {"type": "string"},
        "additions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "you_said": {"type": "string"},
                    "native": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id", "title", "you_said", "native"],
            },
        },
        "graduations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "error_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "error_id", "title", "evidence"],
            },
        },
    },
    "required": ["transcript", "additions", "graduations"],
}


def build(active_errors: list) -> str:
    """Render the prompt with active errors injected so Gemini can spot graduations."""
    parts = [
        "You'll receive a recording of me practicing English with an AI partner.",
        "Analyze MY performance only — don't critique the AI's lines.",
        "If you can't tell us apart, assume I'm the less-confident, less-fluent voice.",
        "",
        "Background: my L1 is Mandarin Chinese (Taiwan). The most common L1-transfer "
        "patterns to expect are: missing articles, dropped 3rd-person -s, present-tense "
        "narration of past events, treating uncountable nouns as countable, "
        "modal/conditional misuse (e.g. 'must' where 'would' is right), and word-by-word "
        "compound nouns ('flash card and app' instead of 'flashcard app'). These are "
        "high-signal targets — flag them aggressively even if the meaning is clear.",
        "",
        "Output JSON in this exact shape:",
        "{",
        '  "transcript": "full dialogue with [Me] and [AI] tags",',
        '  "summary": "1-2 Chinese sentences summarizing the session",',
        '  "fluency_notes": "observations about my fluency, confidence, tone",',
        '  "additions": [',
        '    {"id": "add-1", "title": "short English label naming the error",',
        '     "you_said": "the full sentence I actually said, with the error in place",',
        '     "native": "the same sentence rewritten naturally with the fix applied",',
        '     "note": "Chinese explanation, 1-2 sentences"}',
        "  ],",
        '  "graduations": [',
        '    {"id": "grad-1", "error_id": <integer from the active errors list below>,',
        '     "title": "title copied from the matched error",',
        '     "evidence": "the full sentence where I used the error\'s correct form"}',
        "  ]",
        "}",
        "",
        "Rules:",
        "- you_said and native must be COMPLETE SENTENCES from the transcript, not bare phrases. This applies even when the error is a single noun, article, or modifier — include the whole sentence around it so I remember the context.",
        "- Wrap the specific error portion in double quotes \" \" within you_said, and the corresponding correction in \" \" within native, at the same position. Keeps the diff visually obvious.",
        "  BAD:",
        '    "you_said": "all the infrastructures"',
        '    "native":   "all the infrastructure"',
        "  GOOD:",
        '    "you_said": "I have prepared all the \"infrastructures\" for the app he wanted."',
        '    "native":   "I had prepared all the \"infrastructure\" for the app he wanted."',
        "- If the same grammatical pattern appears in MULTIPLE sentences this session, return ONE addition for the whole pattern, not one per instance. Group them:",
        "  - title: name the pattern, not a single instance",
        "  - you_said: full sentences for each instance, joined by ' / '",
        "  - native: corrected versions in the same order, joined by ' / '",
        "  - note: explain the pattern and mention it recurred",
        "  GOOD grouping example (3 instances of the same past-tense issue collapsed, each with quote-highlighting at the error):",
        '    {"title": "Past tense for past-event narration",',
        '     "you_said": "It was smooth because I have prepared the app he \"want\". / Before I \"leave\" my uncle\'s house, he said... / In my original design, the app only \"provide\" caption.",',
        '     "native":   "It was smooth because I had prepared the app he \"wanted\". / Before I \"left\" my uncle\'s house, he said... / In my original design, the app only \"provided\" caption.",',
        '     "note": "敘述過去事件時動詞要用過去式。本次出現多處。"}',
        "  Different patterns stay separate (past-tense and subject-verb agreement are different patterns even if they involve the same word).",
        "- additions: NEW errors I made this session, not already in active errors",
        "- graduations: STRICT criteria — only include if I demonstrably used the active error's CORRECTED form:",
        "    * the evidence sentence must contain ZERO instance of the error pattern",
        "    * if the evidence sentence still contains the same mistake the error tracks, do NOT graduate",
        "    * a sentence flagged in `additions` CANNOT also appear as graduation evidence — pick one role per sentence",
        "    * when in doubt, omit. A missed graduation is fine. A false graduation undoes real learning.",
        "- note, summary, and fluency_notes must all be in Traditional Chinese (Taiwan)",
        "- Skip these — they are NOT errors worth flagging:",
        "    * accent or pronunciation glitches",
        "    * minor pauses, hesitations, filler words (uh, um)",
        "    * mid-utterance self-corrections: if I started saying something wrong and "
        "fixed it within the same sentence or in the next utterance, the corrected form "
        "stands. Don't flag the slip. Example to skip: 'Anthropic, Anthropic's IPO' — "
        "that's a stutter, not a repetition error.",
        "- IDs are simple sequences (add-1, add-2, grad-1, ...) — don't reuse across categories",
        "",
    ]
    if active_errors:
        parts.append("Active errors (use these IDs in graduations):")
        for e in active_errors:
            body = (e["body_md"] or "")[:200].replace("\n", " ")
            parts.append(f"- id={e['id']} · {e['title']} · {body}")
    else:
        parts.append("Active errors: (none — graduations will be empty)")
    return "\n".join(parts)
