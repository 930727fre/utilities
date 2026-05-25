# free2speak

自製 Speak 替代方案 2.0 — web app + Opus API + SQLite。

用 Gemini app 的 Live 語音對話練習，用 Gemini API 批改錄音，用 Opus API 生成 role-play / drill / 整理錯題本。

## Pages

- **Practice** (`/`) — single-page flow that steps through:
  1. View today's role-play script
  2. Upload recording (Gemini analysis)
  3. Tinder-swipe through new error candidates (add / skip)
  4. Tinder-swipe through old errors that look used-correctly (graduate / keep)
  5. Loop back to step 1 for next session
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

1.0 markdown tree is archived under `1.0-archive/` (`analyze.py`, `prompts/`, `CLAUDE.md`, `README.md`). The `data/` folder is preserved and will be consumed by `import.py` (next chunk of work) to seed the SQLite DB.

## Monthly audit

`python export.py --out /tmp/audit-YYYY-MM/` regenerates a browsable markdown tree from the DB for human / Claude Code review. Read-only, throwaway. Mutations during audit go through the API or `debug.py`, never by editing the exported MD.

## Build status — picking up next session

**Phase 1 done (current state):**
- Backend lifespan calls `init_schema` on startup; DB tables exist on a fresh container.
- `GET /today/stats` is real: practice/drill done-today flags, active errors count, and a computed streak (consecutive days with sessions or completed drills, ending today or yesterday).
- 1.0 archive data has already been imported via `import.py` — DB currently has ~52 active errors.

**Still stubs (return hardcoded data):**
- `GET /today/roleplay` — needs Opus to generate when no row for today
- `POST /upload` — needs Gemini to analyze the audio; should persist a row in `sessions` with `raw_response` JSON containing extracted additions/graduations
- `GET /today/review` — needs to read the latest session's `raw_response` and return its `additions` + `graduations`
- `POST /errors/additions` — needs to resolve `candidate_ids` against the latest session's analysis, then `INSERT INTO errors`
- `POST /errors/graduations` — needs to `UPDATE errors SET status='graduated'` for the given IDs
- `GET /today/drill` — needs Opus to generate when no row for today

**Phase 2 plan (Gemini integration — pick up here):**

1. `POST /upload`: accept audio, call Gemini with the audio + `1.0-archive/prompts/gemini-analysis.md` prompt, expect structured JSON with `{transcript, summary, fluency_notes, additions: [...], graduations: [...]}`. Persist a `sessions` row including `raw_response` (the full JSON dumped as TEXT). Return `{session_id, date, topic}` — topic from today's roleplay row if it exists, else `"(no roleplay)"`.
2. `GET /today/review`: `SELECT raw_response FROM sessions ORDER BY uploaded_at DESC LIMIT 1`, parse JSON, return its `additions` + `graduations`. The frontend's review-swipe screen will then work end-to-end.
3. `POST /errors/additions`: load latest session's `raw_response`, filter additions by `candidate_ids`, insert each into `errors`. Set `source_session_id`, `first_seen_date`, `last_seen_date`, `body_md`.
4. `POST /errors/graduations`: `UPDATE errors SET status='graduated', graduated_at=datetime('now') WHERE id IN (...)`.

**Phase 3 plan (Opus integration):**

1. `GET /today/roleplay`: if a row exists for today's date, return it. Otherwise call Opus with `1.0-archive/prompts/roleplay-generation.md`, passing the active errors + recent sessions metadata. Persist + return.
2. `GET /today/drill`: same pattern but for drills + `drill-generation.md`. Persist the parent `drills` row and child `drill_cards`.

**API keys:** compose already wires `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` from the host shell (default empty). Make sure both are exported before `docker compose up -d --build`.

**Existing helpers:**
- `db.connect()` — opens a fresh sqlite3 connection with row_factory + FK on
- `gemini_client.py` from xyt/flashcard/keyboard is the established pattern for Gemini HTTP calls. For audio upload, use the File API (`files.upload`) or pass `inline_data` with base64 — not the YouTube-URL pattern.
