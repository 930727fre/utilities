"""Gemini audio-transcription prompt + structured-output schema.

Path B (post-2026-07-05) split: Gemini 2.5 Flash's job is now just producing
the transcript — the actual error analysis (additions/graduations/l1_diagnosis)
is done by Claude Sonnet 4.6 downstream, which has stronger English intuition
for collocation/register quality and doesn't fall back to descriptive-prose
diagnoses when there's no clean 1:1 Chinese-word origin.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
    },
    "required": ["transcript"],
}


def build() -> str:
    return "\n".join([
        "You'll receive a recording of me practicing English with an AI partner.",
        "Produce a verbatim transcript of the dialogue only. No commentary, no analysis.",
        "",
        "Format each utterance with a speaker tag:",
        "  [Me] ...my English...",
        "  [AI] ...AI partner's English...",
        "",
        "Rules:",
        "- If you can't tell us apart, assume I'm the less-confident, less-fluent voice.",
        "- Preserve my errors, hesitations, and false-starts as I actually said them "
        "(e.g. 'I have a I have a two reservation'). Don't tidy them.",
        "- Preserve fillers (uh, um) inline.",
        "- One [Me] or [AI] block per utterance/turn; use a new line when the speaker switches.",
        "- Transcript only. Output nothing else in the JSON.",
    ])
