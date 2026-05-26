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
        "- GROUPING — this is critical, get it right:",
        "  Same GRAMMATICAL CATEGORY = same pattern, even if the surface words are different. Different lexical items don't make different patterns. Merge them.",
        "  Examples of what counts as ONE pattern:",
        "    * Uncountable noun treated as countable — 'infrastructures', 'drivers', 'needs', 'captions', 'resources', 'services' are ALL the same pattern. ONE card.",
        "    * Missing article on a first-mention countable noun — 'large language model', 'API', 'lock' (when 'the' is required) — ONE card.",
        "    * Dropped 3rd-person -s — 'he want', 'the tool work', 'it provide' — ONE card.",
        "    * Past-tense for past-event narration — 'I have prepared', 'before I leave', 'app only provide' — ONE card.",
        "  Examples of what stays separate:",
        "    * Tense issues vs. article issues — different categories, different cards.",
        "    * Modal misuse vs. verb-form choice — different categories.",
        "  Grouping format:",
        "    - title: name the CATEGORY, not a single instance (e.g. 'Uncountable noun treated as countable', not 'infrastructures')",
        "    - you_said: full sentences for each instance, joined by ' / ' (cap at ~5 instances; if more, pick the clearest 5 and say so in note)",
        "    - native: corrected versions in the same order, joined by ' / '",
        "    - note: explain the category and mention how many instances",
        "  GOOD grouping example (6 different uncountable nouns collapsed into one card):",
        '    {"title": "Uncountable noun treated as countable",',
        '     "you_said": "I have prepared all the \"infrastructures\". / with Docker and Nvidia \"driver\" installed. / accommodate his \"need\". / the app only provide \"caption\". / won\'t race for GPU \"resource\". / some of my containers are real-time \"service\".",',
        '     "native":   "I had prepared all the \"infrastructure\". / with Docker and Nvidia \"drivers\" installed. / accommodate his \"needs\". / the app only provides a \"caption\". / won\'t race for GPU \"resources\". / some of my containers are real-time \"services\".",',
        '     "note": "中文沒有可數/不可數區分，常把不可數名詞當可數用或漏掉複數。本次出現 6 處。"}',
        "  BAD (DO NOT DO THIS — returning multiple cards for the same category):",
        '    {"title": "infrastructures 誤用為可數", "you_said": "...infrastructures..."}',
        '    {"title": "driver 誤用單數",          "you_said": "...driver..."}',
        '    {"title": "need 誤用單數",            "you_said": "...need..."}',
        "  — that is wrong. They all belong to ONE 'uncountable-as-countable' card. If you find yourself writing similar titles back-to-back, merge them.",
        "  Final check before emitting: scan the additions list. If any two share the same grammatical category, merge them into one card.",
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
