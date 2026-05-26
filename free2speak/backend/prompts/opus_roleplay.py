"""Opus roleplay-generation prompt.

Adapted from `1.0-archive/prompts/roleplay-generation.md`. 1.0 produced freeform
markdown files; 2.0 asks for a structured JSON envelope so we can persist the
script to the DB and return clean fields to the frontend.
"""

# Opus tool-use schema. The model is asked to invoke `emit_roleplay` so we get
# structured output without parsing prose.
TOOL = {
    "name": "emit_roleplay",
    "description": "Emit a generated role-play script for the user's daily practice session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Short slug for the scenario (e.g. 'apartment', 'interview', 'bistro'). 1-2 words.",
            },
            "rationale": {
                "type": "string",
                "description": "One Chinese line explaining what the roleplay drills (which error patterns it surfaces, why this scenario).",
            },
            "body_md": {
                "type": "string",
                "description": "Full bilingual roleplay markdown — AI lines in English (verbatim), user lines as Chinese instructions to translate. 5-7 exchanges. Include a final 'Gemini 開場 prompt' block listing all AI lines for verbatim playback.",
            },
        },
        "required": ["topic", "rationale", "body_md"],
    },
}


def build(active_errors: list, recent_sessions: list, recent_topics: list) -> str:
    """Render the roleplay-generation prompt.

    Args:
      active_errors: rows from `errors` where status='active' — used to drive scenario choice.
      recent_sessions: last ~5 session rows (id, summary, fluency_notes, uploaded_at) for variety.
      recent_topics: last ~10 roleplay topics — avoid repeating.
    """
    parts = [
        "You generate one role-play script per day for a Chinese-L1 English learner (Taiwan, upper-intermediate).",
        "The user runs the script in Gemini Live: the AI plays verbatim, the user reads Chinese instructions and translates them aloud in real time.",
        "",
        "## Format",
        "- 5-7 exchanges. Total spoken time ~3-5 min.",
        "- AI lines: verbatim English (will be played back exactly as written).",
        "- User lines: **ONE primary content hook per exchange**. Be CONCRETE about that single hook so the user can't dodge it with vague phrasing, but DO NOT stack multiple facts.",
        "  GOOD (one focused hook): 「告訴室友你昨天接到 HR 通知，下個月正式升職、加薪 10%」 — forces 'HR notified me / promotion / raise'",
        "  BAD (too abstract): 「分享一個好消息」 — user dodges with 'I have good news'",
        "  BAD (too many hooks stacked): 「告訴她上線後沒有重大 bug，不過你已經追蹤廠商三天了，對方都沒有回音，所以還無法確認 webhook 狀態」 — 4 sub-points in one breath, cognitive overload",
        "  If you need multiple facts, split them across exchanges. The user practices one thing per turn, not four.",
        "- Length: 1 sentence ideal, 2 sentences MAX (only when the second sentence adds the speech act, not more facts).",
        "- AI lines can hook 2-3 concerns per turn — that's fine, the user PICKS which to respond to, but their response stays focused on ONE.",
        "- Tag each user line with a speech act: push back / counter-propose / agree / decline / clarify / etc.",
        "",
        "## Scenario selection",
        "1. Natural fit first — scenario must be plausible in the user's life (Taiwan-based, sometimes US context).",
        "2. Error-driven second — pick a scenario that naturally surfaces patterns from the active errors list below.",
        "3. Don't repeat recent topics (see list).",
        "4. Don't force a scenario to fit errors — better to miss one pattern than to write a contrived setup.",
        "",
        "## body_md structure (write into the body_md field as a single string)",
        "",
        "Do NOT repeat the rationale inside body_md — the rationale field is rendered separately above the body. Start the body at `## Scenario`.",
        "",
        "```",
        "# YYYY-MM-DD — Role-play: [scenario name]（情境分類）",
        "",
        "## Scenario",
        "1-2 sentences of background.",
        "",
        "## Setup",
        "- AI (Gemini): 照 script verbatim 播放、不改詞、不 improv",
        "- 你: 看 Chinese instruction，即時翻成英文（每輪 1-3 句）",
        "",
        "## Dialogue",
        "",
        "### Exchange 1",
        "**AI:** \"...\"",
        "**You (中文):** ... (speech act: ...)",
        "",
        "### Exchange 2",
        "...",
        "",
        "## Gemini 開場 prompt",
        "",
        "(wrap the entire block below in a triple-backtick fenced code block so the user can one-click copy it into Gemini Live)",
        "",
        "You are playing [role] in a SCRIPTED role-play. Read each of your N lines below verbatim, in order. After each line, wait for the user to respond, then proceed to the next line. Do NOT improvise, do NOT paraphrase, do NOT deviate from the script. If the user gets stuck, gently say \"take your time\" and wait.",
        "",
        "Your lines (in order):",
        "1. \"...\"",
        "2. \"...\"",
        "...",
        "```",
        "",
        "Emit the role-play by invoking the `emit_roleplay` tool. Do not output any prose around the tool call.",
        "",
    ]

    if recent_topics:
        parts.append("Recent roleplay topics (avoid repeating these):")
        for t in recent_topics:
            parts.append(f"- {t}")
        parts.append("")

    if active_errors:
        parts.append("Active errors (~recent cluster, prioritize patterns naturally surfaced by your chosen scenario):")
        for e in active_errors[:30]:
            body = (e["body_md"] or "")[:150].replace("\n", " ")
            parts.append(f"- {e['title']} · {body}")
        parts.append("")

    if recent_sessions:
        parts.append("Recent sessions (for variety — don't repeat scenario shape):")
        for s in recent_sessions[:5]:
            summary = (s.get("summary") or "").replace("\n", " ")[:200]
            parts.append(f"- {s.get('uploaded_at', '')}: {summary}")
        parts.append("")

    return "\n".join(parts)
