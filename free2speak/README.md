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

**Phase 1 done — DB layer:**
- Backend lifespan calls `init_schema` on startup; DB tables exist on a fresh container.
- `GET /today/stats` is real: practice/drill done-today flags, active errors count, and a computed streak (consecutive days with sessions or completed drills, ending today or yesterday).
- 1.0 archive data has already been imported via `import.py` — DB currently has ~52 active errors.

**Phase 2 done — Gemini audio analysis:**
- `POST /upload` accepts audio (≤20 MB), inline-base64 it to Gemini 2.5 Flash with the prompt in `main.py:_build_analysis_prompt`. Structured-output JSON schema enforces shape: `{transcript, summary, fluency_notes, additions[], graduations[]}`. Active errors (up to 100) are injected into the prompt so Gemini can flag graduations by their DB ID. Session row persisted with the raw JSON in `raw_response`; audio file saved under `/data/sessions/{id}.{ext}`.
- `GET /today/review` reads the latest session's `raw_response` and returns its `additions` + `graduations` (frontend's swipe screens consume this directly).
- `POST /errors/additions` resolves frontend candidate IDs against the latest session's analysis and `INSERT`s real rows into `errors`. `body_md` is rendered from `{you_said, native, note}`.
- `POST /errors/graduations` maps frontend `grad-N` IDs back to the DB `error_id` via the latest session's analysis, then `UPDATE`s status to `graduated`.

**Phase 3 plan (Opus integration — pick up here):**

1. `GET /today/roleplay`: if a row exists for today's date, return it. Otherwise call Opus (Anthropic SDK already in `requirements.txt`) with `1.0-archive/prompts/roleplay-generation.md`, passing the active errors + recent sessions metadata. Persist + return.
2. `GET /today/drill`: same pattern but for drills + `drill-generation.md`. Persist the parent `drills` row and child `drill_cards`.
3. Consider a separate post-session "errors-generation" pass using `errors-generation.md` if the Gemini per-session detection turns out too noisy.

**API keys:** compose already wires `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` from the host shell (default empty). Make sure both are exported before `docker compose up -d --build`. Gemini is required for `/upload` to work; Anthropic only needed in Phase 3.

**Existing helpers (Phase 2 era):**
- `db.connect()` — opens a fresh sqlite3 connection with row_factory + FK on.
- `_build_analysis_prompt(active_errors)` and `_call_gemini_with_audio(audio, mime, prompt)` in `main.py` — reuse the latter as a template for Opus calls in Phase 3 (swap base URL, schema, and auth header).

**Limitations to revisit:**
- 20 MB inline ceiling on audio — long recordings need the Gemini Files API.
- Per-call active-error cap of 100 — fine until the error book grows large; then consider top-N by recency/relevance instead of by `last_seen_date` alone.
- Audio files accumulate under `/data/sessions/` indefinitely. No cleanup yet.
