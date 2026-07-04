# free2speak

自製 Speak 替代方案 2.0 — web app + Opus API + 檔案系統儲存。

用 Gemini app 的 Live 語音對話練習，用 Gemini API 批改錄音，用 Opus API 生成 role-play / drill / 整理錯題本。

## Pages

- **Practice** (`/`) — single-page flow that steps through:
  1. View the currently active role-play script. Two paths out:
     - **Done practicing** → upload in `mode='roleplay'` (consumes this roleplay when review completes)
     - **Skip (free chat)** → upload in `mode='freestyle'` (roleplay stays active for next time)
  2. Upload recording (Gemini analysis)
  3. Tinder-swipe through new error candidates (add / skip) — *each swipe persists immediately*
  4. Tinder-swipe through old errors that look used-correctly (graduate / keep) — *each swipe persists immediately*
  5. Loop back to step 1: if it was roleplay-mode, a fresh active roleplay is generated; if freestyle, the same roleplay is still there
  - **Resume**: closing the tab mid-flow doesn't lose state. On next visit, `/today/practice/state` lands you on the next undecided card.
- **Drill** (`/drill`) — Tinder-swipe stack of drill cards. Tap to flip and reveal answer. No rating / no state mutation.

## Architecture

- Backend: FastAPI + filesystem + Opus API + Gemini API.
- Frontend: React + Vite + Mantine + TypeScript, served by nginx with `/api/` reverse-proxied to backend.
- Data: `data/` bind-mount is the source of truth. No DB, no schema, no migrations.

### File-system layout

```
data/
├── errors/{active,graduated}/NNNN-<slug>.md
│                                # front-matter: id, status, dates, source_session_id
│                                # body: **you_said**, **native**, **register**, **l1_diagnosis**, **note**
├── roleplays/{active,done}/<date>-<topic>[-<hash>].md
│                                # front-matter: id, date, topic, rationale, status
│                                # body: full bilingual script + Gemini 開場 prompt block
├── sessions/
│   ├── <sid32>.<ext>            # raw audio (mp3 / m4a / webm — mime-derived)
│   ├── <sid32>.json             # {mode, roleplay_id, uploaded_at, transcript, summary,
│   │                            #  fluency_notes, raw_response}
│   └── <sid32>.decisions.jsonl  # append-only: {candidate_id, action, at} per swipe
├── drills/<YYYY-MM-DD>.json     # {rationale, cards[], created_at}
└── legacy/                       # 1.0-archive markdown tree — never touched at runtime
```

### Invariants encoded by the filesystem

- **≤1 active roleplay**: `roleplays/active/` contains at-most-one file. No DB partial unique index needed — the directory is the invariant.
- **Review completion is derived**: no `review_done` flag on disk. `is_review_done(sid)` = every candidate id in `<sid>.json`'s `raw_response.additions + graduations` has a line in `<sid>.decisions.jsonl`. State is a pure function of files.
- **Idempotent graduation**: `mv errors/active/NNNN-*.md errors/graduated/`. Re-running is a no-op.

### Loading rules per Opus call

- `errors`: **always full** (bounded by design)
- `sessions`: recent 5 only
- `roleplays` / `drills` history: metadata only (date + topic + rationale)

## Deploy

```bash
export ANTHROPIC_API_KEY=...
export GEMINI_API_KEY=...
docker compose up -d --build
```

Then register `free2speak` subdomain in Cloudflare tunnel dashboard pointing to `free2speak-frontend:80` on `my_network`.

## Migration from SQLite (2.0.x → 2.1)

The 2.0.x line stored state in `data/free2speak.db`. To migrate:

```bash
docker exec free2speak-backend python /app/migrate_db_to_files.py
```

This sidelines 1.0-era files to `data/legacy/` and dumps every DB row to the new layout. Verify the UI still works, then:

```bash
rm data/free2speak.db
```

The migration script is idempotent; can be re-run without harm.

## Rollback / debug

No `debug.py`, no SQL. Just `rm` and `mv`:

- **Delete a bad session**: `rm data/sessions/<sid>.*`
- **Un-graduate an error**: `mv data/errors/graduated/NNNN-*.md data/errors/active/`
- **Revive a done roleplay**: `mv data/roleplays/done/<file>.md data/roleplays/active/` (make sure `active/` is empty first)
- **Nuke the day's drill for regeneration**: `rm data/drills/YYYY-MM-DD.json`

Editing a card = `vim data/errors/active/NNNN-*.md`. Front-matter stays intact if you don't touch the `---` delimiters.

## Monthly audit

```bash
grep -r "reservation" data/errors/     # find all cards mentioning a phrase
ls data/roleplays/done/ | wc -l         # count consumed roleplays
cat data/drills/2026-07-*.json | jq '.rationale'  # scan month's drill rationales
```

## Build status

**Phase 1 — DB layer:** ✅ shipped as SQLite, later refactored to filesystem in 2.1.

**Phase 2 — Audio analysis (two-pass):** ✅
- `POST /upload` accepts audio (≤20 MB) + a `mode='roleplay'|'freestyle'` form field. Path B pipeline:
  1. **Gemini 2.5 Flash** transcribes the audio → verbatim `[Me]/[AI]`-tagged text (`prompts/gemini_transcribe.py`). Fast + cheap, no analysis.
  2. **Claude Sonnet 4.6** analyzes the transcript with all L1-diagnosis / grouping / register rules (`prompts/claude_analyze.py`). Sonnet's English intuition avoids Flash's fallback-to-descriptive-prose failure mode on cards without a clean 1:1 Chinese-word origin, and produces better idiomatic `native` fixes (`hear back` vs `information`, `days off` vs `periods of leave`).
- Final `raw_response` shape unchanged: `{transcript, summary, fluency_notes, additions[], graduations[]}` — downstream (review, decide, drill) doesn't know it was two calls.
- `additions[]` items carry `title`, `you_said`, `native`, `register` (where the native form belongs), `l1_diagnosis` (Chinese sentence explaining the L1-transfer mechanism), `note` (grouping/instance context). `register` + `l1_diagnosis` are the noticing-hypothesis payload — enforces contrastive noticing on lexical L1-transfer.
- `GET /today/review` returns only the **undecided** additions + graduations from the latest pending session.
- `POST /sessions/{id}/decide` body `{candidate_id, action}` — per-card persistence via append to `<sid>.decisions.jsonl`. `action='added'` writes a new error file; `'graduated'` moves the matching active error to `errors/graduated/`. When all candidates are decided, the linked roleplay (if roleplay-mode) transitions to `done/`.
- `GET /today/practice/state` returns the step the frontend should land on (`'roleplay'`, `'additions'`, or `'graduations'`) plus a `session_id` for resume.

**Phase 3 — Opus roleplay + drill generation:** ✅
- `GET /today/roleplay`: returns the active roleplay (single file in `roleplays/active/`). If empty, calls Opus (tool-use with the `emit_roleplay` schema) to generate a 5-7 exchange bilingual script and writes it into `roleplays/active/`. Body is full markdown; front-matter has the metadata. Active errors + recent sessions + recent topics passed as context.
- `GET /today/drill`: date-keyed. Opus generates 10 cards. Persists as `data/drills/YYYY-MM-DD.json`.

**API keys:** compose fails parse if either `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` is missing from the host shell.

**Code layout:**
```
backend/
├── main.py                       # endpoints (lifespan, stats, roleplay, upload, practice/state, review, decide, drill)
├── storage.py                    # filesystem access layer — the only module that touches /data
├── opus_client.py                # Anthropic tool-use wrapper
├── prompts/
│   ├── gemini_analysis.py        # audio → additions+graduations JSON
│   ├── opus_roleplay.py          # active_errors + recent_sessions → script
│   └── opus_drill.py             # active_errors + recent_sessions → 10 cards
├── models.py                     # pydantic shapes
└── migrate_db_to_files.py        # one-shot SQLite → files (kept for reference; delete after verified migration)
```

**Limitations to revisit (none blocking):**
- 20 MB inline ceiling on audio uploads — long recordings need the Gemini Files API.
- Per-call active-error cap of 100 — fine until the error book grows large.
- Audio files accumulate under `data/sessions/` indefinitely. No cleanup yet.
- Drill `source_error_id` returned as string to the frontend. Frontend doesn't actually use the value yet.
- No regressions endpoint — active errors that still fail in a session aren't surfaced; only the correct-uses graduations are.
