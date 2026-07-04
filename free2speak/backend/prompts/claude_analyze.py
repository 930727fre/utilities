"""Claude Sonnet 4.6 analysis prompt + tool-use schema.

Path B (post-2026-07-05): takes a plain-text transcript (produced by Gemini
2.5 Flash upstream) plus active errors, and returns the structured analysis
that used to come out of Gemini directly:
  {summary, fluency_notes, additions[], graduations[]}

Why Sonnet vs Gemini Flash: Flash was falling back to descriptive-prose
`l1_diagnosis` when a card didn't have a clean single-word Chinese origin
(e.g. 'on the other week', 'X-ray review'). Sonnet's stronger English
intuition + more nuanced instruction-following extrapolates the L1-story
rule to non-obvious cases. It also produces better `native` fixes for
collocation-level errors (hear back vs "information", days off vs
"periods of leave") that Flash mangles.

Cost: ~$0.05/call at Sonnet 4.6 pricing (5k in + 2.5k out tokens).
"""

TOOL = {
    "name": "emit_analysis",
    "description": "Return the structured analysis of the user's English practice recording.",
    "input_schema": {
        "type": "object",
        "properties": {
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
                        "register": {"type": "string"},
                        "l1_diagnosis": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["id", "title", "you_said", "native", "l1_diagnosis"],
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
        "required": ["summary", "fluency_notes", "additions", "graduations"],
    },
}


def build(transcript: str, active_errors: list) -> str:
    """Render the analysis prompt.

    Args:
      transcript: [Me]/[AI]-tagged verbatim text from the Gemini transcribe pass.
      active_errors: rows from storage.list_active_errors() — used to score graduations.
    """
    parts = [
        "You're reviewing a transcript of me (a Chinese-L1 English learner) practicing "
        "English with an AI partner. My performance only — don't critique the AI's lines.",
        "",
        "Background: my L1 is Mandarin Chinese (Taiwan). The most common L1-transfer "
        "patterns to expect are: missing articles, dropped 3rd-person -s, present-tense "
        "narration of past events, treating uncountable nouns as countable, "
        "modal/conditional misuse (e.g. 'must' where 'would' is right), and word-by-word "
        "compound nouns ('flash card and app' instead of 'flashcard app'). These are "
        "high-signal targets — flag them aggressively even if the meaning is clear.",
        "",
        "The most persistent problem: upper-intermediate Chinglish. My sentences are "
        "grammatically correct but I pick the wrong lexical item via Chinese→English "
        "dictionary lookup (預約→reservation, 修改→modification, 白天→on the day, "
        "撞到→collide, 核准→validated, 回饋→feedback). Fixing this requires explicit "
        "contrastive noticing on WHY the wrong word felt right in Chinese but doesn't "
        "work in English — not just showing the correction. This is the noticing-hypothesis "
        "payload that `register` + `l1_diagnosis` carry.",
        "",
        "## Transcript",
        "",
        transcript.strip(),
        "",
        "## Output shape",
        "",
        "Invoke the `emit_analysis` tool with:",
        "  summary: 1-2 Traditional-Chinese sentences summarizing the session.",
        "  fluency_notes: Traditional-Chinese observations about my fluency, confidence, tone.",
        "  additions: NEW error cards for this session (not already in active errors — see list below).",
        "  graduations: active errors I demonstrably used correctly this session.",
        "",
        "## additions[] rules",
        "",
        "Each item:",
        "  id: 'add-1', 'add-2', ...",
        "  title: short English label naming the pattern (not a single instance).",
        "  you_said: full sentence from the transcript with the error, error portion "
        "wrapped in \"double quotes\".",
        "  native: same sentence rewritten naturally with the fix, corrected portion "
        "wrapped in \"double quotes\" at the matching position.",
        "  register: where the native form actually lives (short phrase, ≤6 words).",
        "  l1_diagnosis: one Chinese sentence explaining the Chinese thinking pattern that "
        "caused the error, referencing THIS session's specific mismap.",
        "  note: instance count or grouping context (Traditional Chinese).",
        "",
        "### you_said / native format",
        "",
        "MUST be complete sentences from the transcript, not bare phrases. This applies "
        "even when the error is a single word — include the whole sentence for context.",
        "",
        "  BAD:",
        '    "you_said": "all the infrastructures"',
        '    "native":   "all the infrastructure"',
        "  GOOD:",
        '    "you_said": "I have prepared all the \"infrastructures\" for the app he wanted."',
        '    "native":   "I had prepared all the \"infrastructure\" for the app he wanted."',
        "",
        "`you_said` = what I actually said, PRESERVING my disfluencies (uh, um, false starts, "
        "self-corrections like 'I have a I have a'). Preserve them because they're my authentic "
        "speech and I want to see myself.",
        "",
        "`native` = what a fluent English speaker WOULD have said in the same situation — clean, "
        "idiomatic, one sentence with no false starts and no filler. Don't just minimally patch the "
        "underlined word; rewrite the whole sentence naturally, because a minimal patch often "
        "yields ungrammatical output (e.g. 'two reservation' → 'two a reservations' is broken).",
        "",
        "  BAD native rewrites:",
        '    you_said: "I have a I have a two \"reservation\" at 3 p.m."',
        '    native:   "I have a I have a two \"reservations\" at 3 p.m."    ← preserved stutter + \"a\" article clash with \"two\"',
        "",
        "  GOOD:",
        '    you_said: "I have a I have a two \"reservation\" at 3 p.m."',
        '    native:   "I have two \"appointments\" at 3 p.m."    ← clean idiomatic version',
        "",
        "  When wrapping the corrected portion in \"double quotes\" in the clean `native` version, "
        "wrap the equivalent semantic span — not necessarily the same string position. The reader "
        "compares `you_said` red highlight with `native` accent highlight to notice the swap.",
        "",
        "### GROUPING",
        "",
        "Grammar cards: group by RULE VIOLATED. Same rule = same card, even across different words.",
        "  * Uncountable noun treated as countable — 'infrastructures', 'drivers', 'services'  → ONE card.",
        "  * Missing article on first-mention countable noun  → ONE card.",
        "  * Dropped 3rd-person -s — 'he want', 'the tool work'  → ONE card.",
        "  * Past-tense for past events — 'I have prepared', 'app only provide'  → ONE card.",
        "",
        "Lexical cards: group by SPECIFIC Chinese→English mismap. 'L1 lexical transfer' is NOT "
        "a single category — 預約→reservation, 修改→modification, 白天→on-the-day, 撞到→collide, "
        "回饋→feedback, 核准→validated are SIX different cards. Merge only when the SAME wrong "
        "L2 word (or SAME L1 origin) recurs.",
        "",
        "DIFFERENT rules stay SEPARATE cards. Do NOT merge across rules:",
        "  * Missing plural -s vs. uncountable-as-countable — different rules, different cards. "
        "'two reservation' (missing plural on a countable noun) and 'several leaves' (uncountable "
        "made countable) are TWO cards, not one.",
        "  * Redundant word vs. missing article — different rules. 'last last cleaning' (redundant) "
        "and 'last cleaning' missing 'the' are two separate cards even though they overlap on the "
        "same phrase.",
        "  * Tense issues vs. article issues — different cards.",
        "  * Modal misuse vs. verb-form choice — different cards.",
        "",
        "### Grouping-card format",
        "",
        "  title: name the RULE / CATEGORY, not a single instance ('Uncountable noun treated as "
        "countable', not 'infrastructures').",
        "  you_said: full sentences for each instance, joined by ' / ' (cap ~5; if more, pick the "
        "clearest 5 and mention count in note).",
        "  native: corrected versions in the same order, joined by ' / '.",
        "  note: 'X 處' instance count.",
        "",
        "  GOOD grouping (6 uncountable-as-countable collapsed):",
        '    {"title": "Uncountable noun treated as countable",',
        '     "you_said": "I have prepared all the \"infrastructures\". / with Docker and Nvidia \"driver\" installed. / accommodate his \"need\".",',
        '     "native":   "I had prepared all the \"infrastructure\". / with Docker and Nvidia \"drivers\" installed. / accommodate his \"needs\".",',
        '     "note": "中文沒有可數/不可數區分，常把不可數名詞當可數用或漏掉複數。本次出現 3 處。"}',
        "",
        "  BAD (returning multiple cards for the same rule):",
        '    {"title": "infrastructures 誤用為可數", ...}',
        '    {"title": "driver 誤用單數",          ...}',
        "  — WRONG. Same rule = one card. If you write similar titles back-to-back, merge them.",
        "",
        "### register field",
        "",
        "Where the native form actually lives. Fill for vocab/lexical/collocation errors. Leave "
        "empty for pure grammar errors (missing article, dropped -s, tense drift).",
        "",
        "Format: ONE short noun phrase, ≤6 words. Not multi-slash. Not phrase mixed with verbs.",
        "  Good: 'medical / professional appointments', 'informal customer-service', 'scheduling', 'insurance / medical billing'",
        "  Bad:  'informal customer-service reply' (phrase mixed with action), 'medical / customer / conversation / insurance' (multi-slash)",
        "  If a grouping card genuinely spans registers (rare), write 'mixed' — don't slash-list.",
        "",
        "### l1_diagnosis field",
        "",
        "One Chinese sentence pinpointing the Chinese thinking pattern that caused THIS session's "
        "error. ALWAYS fill for every card, including grammar cards. Don't repeat the correction — "
        "diagnose the thinking.",
        "",
        "Quality bar: MUST reference THIS session's specific mismap. Naming the pattern in the "
        "abstract ('中文動詞不變化') is not enough — you must cite the mechanism that produced "
        "this session's mistakes.",
        "",
        "Good l1_diagnosis:",
        '  vocab: "中文『預約』一個詞蓋餐廳/機票/醫療；英文分工 reservation/booking/appointment，醫療專用 appointment。"',
        '  collocation: "中文『白天』對應 daytime，但『白天接不到電話』的自然英文是 during the day，on the day 指某個特定日子。"',
        '  grammar (past tense): "中文動詞不變化，靠時間副詞（already, before, last time）暗示時態。這次 \'I file the document\' 就是把過去動作用原形交代 — 中文『我上次寄文件』動詞本身不帶時態，直譯過來就漏掉 -ed。"',
        '  grammar (uncountable): "中文沒有可數/不可數區分，講 abstract 名詞習慣加量詞（一份 feedback、一次 leave），直譯過來就把 uncountable 當可數。這次 leaves / feedbacks 就是這個路徑。"',
        '  register-transfer: "中文『修改預約』的『修改』是正式詞，英文對應 modification 屬公文語體；日常客服對話用 change。"',
        "",
        "Bad l1_diagnosis:",
        '  "should use \'appointment\'" — this is just the correction.',
        '  "英文有許多固定搭配和慣用語，不能直接逐字翻譯。" — pure template, no L1 story.',
        '  "中文動詞不變化，講述過去事件時容易混淆英文時態。" — names the pattern but doesn\'t explain THIS session\'s mechanism.',
        '  "\'on the other week\' 在英文中通常指過去某個不特定的週，應使用 \'the following week\'。" — descriptive prose about English usage, NOT a Chinese thinking story. Fix: rewrite as "中文『另外一週』or『下一週』的直譯是 on the other week；英文自然說法是 the following week / the week after，因為 on the day 系列固定指特定某日不能推廣到週。"',
        "",
        "If a specific error genuinely has no clean 1:1 Chinese origin, still frame it as an L1 "
        "cognitive habit — 'this is a general Chinglish tendency: constructing English phrases word-by-word "
        "from Chinese building blocks instead of retrieving them as fixed collocations' + name the "
        "specific building blocks. Don't default to English-usage description.",
        "",
        "### native rewrite quality",
        "",
        "The `native` field is the noticing-hypothesis payload the user will see when swiping. "
        "It MUST use natural spoken/written English at the target register, not overly-formal or "
        "translation-ese rewrites.",
        "",
        "Bad native rewrites:",
        '  "haven\'t received any information" for 沒回音 → say "haven\'t heard back" or "haven\'t gotten a response".',
        '  "several periods of leave" for 請了幾次假 → say "several days off" or "a lot of time off".',
        "",
        "Prefer idiomatic collocations over technically-correct-but-stilted rewrites.",
        "",
        "## graduations[] rules",
        "",
        "STRICT criteria — only include if I demonstrably used the active error's CORRECTED form:",
        "  * the evidence sentence must contain ZERO instance of the error pattern.",
        "  * if the evidence sentence still contains the same mistake, do NOT graduate.",
        "  * a sentence flagged in `additions` CANNOT also appear as graduation evidence — pick one role per sentence.",
        "  * when in doubt, omit. A missed graduation is fine. A false graduation undoes real learning.",
        "  * evidence.error_id must reference an id from the Active errors list below.",
        "",
        "## Skips",
        "",
        "NOT errors worth flagging:",
        "  * accent or pronunciation glitches.",
        "  * minor pauses, hesitations, filler words (uh, um).",
        "  * mid-utterance self-corrections: if I started saying something wrong and fixed it within "
        "the same sentence or in the next utterance, the corrected form stands. Don't flag the slip. "
        "Example to skip: 'Anthropic, Anthropic's IPO' — stutter, not error.",
        "",
        "## Language",
        "",
        "note, summary, fluency_notes, l1_diagnosis all in Traditional Chinese (Taiwan).",
        "",
        "## IDs",
        "",
        "Simple sequences (add-1, add-2, ...; grad-1, grad-2, ...) — don't reuse across categories.",
        "",
    ]
    if active_errors:
        parts.append("## Active errors")
        parts.append("")
        parts.append("Use these IDs in graduations:")
        for e in active_errors:
            body = (e.get("body_md") or "")[:200].replace("\n", " ")
            parts.append(f"- id={e['id']} · {e.get('title', '')} · {body}")
    else:
        parts.append("## Active errors")
        parts.append("(none — graduations will be empty)")
    parts.append("")
    parts.append("Emit the analysis by invoking the `emit_analysis` tool. Do not output any prose around the tool call.")
    return "\n".join(parts)
