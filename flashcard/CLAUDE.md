# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
npm run dev       # Start Vite dev server
npm run build     # TypeScript check + production build
npm run lint      # ESLint validation
npm run preview   # Preview production build
```

There is no test suite.

## Architecture

**Stack:** React 19 + TypeScript + Vite, TanStack Query for server state, Mantine 9 for UI, HashRouter for routing.

**Backend:** FastAPI (Python) with SQLite at `/data/flashcard.db`. Two tables: `cards` (16 columns, FSRS metrics) and `settings` (key-value). FSRS scheduling is handled server-side by `py-fsrs`. Streak logic runs in Asia/Taipei timezone.

**Data flow:**
1. Pages use TanStack Query to fetch from the REST API via `src/api.ts`.
2. Review session: `GET /cards/queue` returns due cards first, then new cards up to the daily cap (20).
3. Rating submission: `POST /cards/{id}/review` — backend applies py-fsrs and returns the rescheduled card.
4. Streak is evaluated server-side on every `GET /stats` and `GET /settings` call.

**Key files:**
- `src/api.ts` — All HTTP calls (`getStats`, `getQueue`, `searchCards`, `batchAddCards`, `reviewCard`, `updateCard`)
- `src/types.ts` — `Card`, `Stats`, `Queue` interfaces
- `src/App.tsx` — Route definitions + MantineProvider + QueryClientProvider
- `backend/main.py` — FastAPI app; all endpoints and streak/FSRS logic
- `backend/models.py` — Pydantic v2 models (`Card`, `CardUpdate`, `Settings`, `ReviewRequest`, `SyncPayload`)
- `nginx/nginx.conf` — Serves static build on :80; proxies `/api/` → `flashcard-backend:8000/`

**Routing (HashRouter):** `/` → DashboardPage, `/review` → ReviewPage, `/batch-add` → BatchAddPage, `/edit` → EditPage

**Card state machine:** `state: 0|1|2|3` = New / Learning / Review / Relearning (FSRS spec).

**Batch import format (BatchAddPage):** One card per line, `::` separated: `word::note::sentence`
