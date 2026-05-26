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

## Build status

**Phase 1 done — DB layer:**
- Backend lifespan calls `init_schema` on startup; DB tables exist on a fresh container.
- `GET /today/stats` is real: practice/drill done-today flags, active errors count, and a computed streak (consecutive days with sessions or completed drills, ending today or yesterday).
- 1.0 archive data has already been imported via `import.py` — DB currently has ~52 active errors.

**Phase 2 done — Gemini audio analysis:**
- `POST /upload` accepts audio (≤20 MB), inline-base64 it to Gemini 2.5 Flash with the prompt in `prompts/gemini_analysis.py:build`. Structured-output JSON schema enforces shape: `{transcript, summary, fluency_notes, additions[], graduations[]}`. Active errors (up to 100) are injected into the prompt so Gemini can flag graduations by their DB ID. Session row persisted with the raw JSON in `raw_response`; audio file saved under `/data/sessions/{id}.{ext}`.
- `GET /today/review` reads the latest session's `raw_response` and returns its `additions` + `graduations` (frontend's swipe screens consume this directly).
- `POST /errors/additions` resolves frontend candidate IDs against the latest session's analysis and `INSERT`s real rows into `errors`. `body_md` is rendered from `{you_said, native, note}`.
- `POST /errors/graduations` maps frontend `grad-N` IDs back to the DB `error_id` via the latest session's analysis, then `UPDATE`s status to `graduated`.

**Phase 3 done — Opus roleplay + drill generation:**
- `GET /today/roleplay`: returns today's row if exists, otherwise calls Opus (tool-use with the `emit_roleplay` schema) to generate a 5-7 exchange bilingual script. Body is full markdown stored in `roleplays.body_md`. Active errors + recent sessions + recent topics passed as context so the scenario fits the user's current error cluster without repeating.
- `GET /today/drill`: same pattern. Opus generates 10 cards (`~7 from active errors + ~3 from recent session content`, mix of fill_blank and translate). Persists parent `drills` row + 10 child `drill_cards` rows. Returns the cards sorted by `order_index`.
- Code layout: `prompts/opus_roleplay.py` + `prompts/opus_drill.py` each define a `TOOL` (Anthropic input_schema for forced structured output) and a `build(...)` for prompt rendering. `opus_client.py` is a thin Anthropic SDK wrapper.

**API keys:** compose fails parse if either `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` is missing from the host shell.

**Code layout (final):**
```
backend/
├── main.py                       # endpoints + helpers (lifespan, stats, upload, review, additions/graduations, roleplay, drill)
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
