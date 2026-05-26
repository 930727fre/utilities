# free2speak

自製 Speak 替代方案 2.0 — web app + Opus API + SQLite。

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

- Backend: FastAPI + SQLite + Opus API + Gemini API. Single writer (no concurrent write contention).
- Frontend: React + Vite + Mantine + TypeScript, served by nginx with `/api/` reverse-proxied to backend.
- Data: `data/free2speak.db` (bind-mounted, snapshotted nightly by the backup tool).

### Tables

- `errors` — active error pool (auto-pruned via graduations)
- `sessions` — practice records (transcript + Gemini analysis JSON)
- `roleplays` — generated role-play scripts (with `rationale` column)
- `drills` — generated drill cards (with `rationale` column)

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

## Migration from 1.0

1.0 markdown tree is archived under `1.0-archive/` (`analyze.py`, `prompts/`, `CLAUDE.md`, `README.md`). The `data/` folder is preserved as a source of truth; `backend/import.py` reads from it (`errors.md`, `roleplays/*.md`, `sessions/*.json`, `drills/*.md`) and seeds the SQLite DB. Re-runnable any time by deleting `data/free2speak.db` first (the importer refuses to run on a non-empty DB).

## Monthly audit

`python export.py --out /tmp/audit-YYYY-MM/` regenerates a browsable markdown tree from the DB for human / Claude Code review. Read-only, throwaway. Mutations during audit go through the API or `debug.py`, never by editing the exported MD.

## Build status

**Phase 1 done — DB layer:**
- Backend lifespan calls `init_schema` on startup; DB tables exist on a fresh container.
- `GET /today/stats` is real: practice/drill done-today flags, active errors count, and a computed streak (consecutive days with sessions or completed drills, ending today or yesterday).
- 1.0 archive data has already been imported via `import.py` — DB currently has ~52 active errors.

**Phase 2 done — Gemini audio analysis:**
- `POST /upload` accepts audio (≤20 MB) + a `mode='roleplay'|'freestyle'` form field. Audio is inline-base64'd to Gemini 2.5 Flash with the prompt in `prompts/gemini_analysis.py:build`. Structured-output JSON schema enforces shape: `{transcript, summary, fluency_notes, additions[], graduations[]}`. Active errors (up to 100) are injected so Gemini can flag graduations by their DB ID. Session row persisted with `mode`, the raw JSON in `raw_response`, an empty `decisions={}` JSON map, and (for roleplay mode) the active roleplay's id as a foreign key.
- `GET /today/review` returns only the **undecided** additions + graduations from the latest pending session (filtered against `session.decisions`).
- `POST /sessions/{id}/decide` body `{candidate_id, action}` — per-card persistence. `action='added'` inserts the error row; `'graduated'` flips the matching active error to graduated; `'skipped'` / `'kept'` just record the decision. When all candidates are decided, the session is finalized (`review_done=1`) and — if the session was roleplay-mode — the linked roleplay transitions to `status='done'`.
- `GET /today/practice/state` returns the step the frontend should land on (`'roleplay'`, `'additions'`, or `'graduations'`) plus a `session_id` for resume. Drives no-data-loss reload behavior: close the tab mid-swipe, come back later, pick up at the next undecided card.

**Phase 3 done — Opus roleplay + drill generation:**
- `GET /today/roleplay`: returns the **currently active roleplay** (`WHERE status='active'`, partial-unique-indexed for at-most-one). If none exists, calls Opus (tool-use with the `emit_roleplay` schema) to generate a 5-7 exchange bilingual script and inserts as the new active row. Body is full markdown stored in `roleplays.body_md`. Active errors + recent sessions + recent topics passed as context so the scenario fits the user's current error cluster without repeating. A roleplay persists across days until a roleplay-mode session consumes it (or the user manually flips it to `done`).
- `GET /today/drill`: same pattern but date-keyed (one drill per day). Opus generates 10 cards (`~7 from active errors + ~3 from recent session content`, mix of fill_blank and translate). Persists parent `drills` row + 10 child `drill_cards` rows. Returns the cards sorted by `order_index`.
- Code layout: `prompts/opus_roleplay.py` + `prompts/opus_drill.py` each define a `TOOL` (Anthropic input_schema for forced structured output) and a `build(...)` for prompt rendering. `opus_client.py` is a thin Anthropic SDK wrapper.

**API keys:** compose fails parse if either `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` is missing from the host shell.

**Code layout (final):**
```
backend/
├── main.py                       # endpoints + helpers (lifespan, stats, roleplay, upload, practice/state, review, decide, drill)
├── db.py                         # sqlite connection + schema init
├── opus_client.py                # Anthropic tool-use wrapper
├── prompts/
│   ├── gemini_analysis.py        # audio → additions+graduations JSON
│   ├── opus_roleplay.py          # active_errors + recent_sessions → script
│   └── opus_drill.py             # active_errors + recent_sessions → 10 cards
├── models.py                     # pydantic shapes
├── schema.sql                    # tables (idempotent CREATE IF NOT EXISTS)
└── import.py                     # one-shot 1.0 → 2.0 importer
```

**Limitations to revisit (none blocking):**
- 20 MB inline ceiling on audio uploads — long recordings need the Gemini Files API.
- Per-call active-error cap of 100 — fine until the error book grows large.
- Audio files accumulate under `/data/sessions/` indefinitely. No cleanup yet.
- Drill `source_error_id` returned as string to the frontend (matches stub contract). DB stores it as integer. Frontend doesn't actually use the value yet.
- No regressions endpoint — active errors that still fail in a session aren't surfaced; only the correct-uses graduations are. Add if "am I still failing this?" feedback becomes annoying.
