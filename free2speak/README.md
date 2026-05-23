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
