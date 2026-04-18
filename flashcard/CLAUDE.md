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

**Stack:** React 19 + TypeScript + Vite, Zustand for state, Mantine 9 for UI, HashRouter for routing.

**Backend:** The app talks to a Google Apps Script (GAS) deployment URL stored in `localStorage`. `Code.gs` is the backend that wraps a Google Sheet as a database. There is no traditional REST API — all reads go through `gasApi.getAll()` (a GET with query params), and all writes go through `gasPost()` which uses `mode: 'no-cors'` to work around CORS limitations (meaning write responses are opaque/unreadable).

**Data flow:**
1. On load, `useStore().fetchEverything()` fetches today's review queue + settings from GAS.
2. If the review queue is empty and no new cards were picked up today, it auto-fetches new cards.
3. Pages read from the Zustand store; mutations call both the store update method and an async GAS POST (fire-and-forget).

**Key files:**
- `src/store.ts` — Zustand store; all app state lives here including `gasUrl`, `cards`, `settings`, `isLoading`
- `src/api.ts` — All GAS communication (`gasApi.*` for reads, `gasPost()` for writes)
- `src/types.ts` — `Card` and `Settings` interfaces
- `src/lib/fsrs.ts` — Wraps `ts-fsrs`; `createFSRSCard()` converts internal Card to FSRS format, `computeNext()` calculates next review state from a Rating (1–4)
- `src/App.tsx` — Route definitions; redirects `/` to `/settings` if no GAS URL is configured

**Routing (HashRouter):** `/` → Dashboard, `/settings` → SettingsPage, `/review` → ReviewPage, `/batch-add` → BatchAddPage

**Card state machine:** Cards have `state: 0|1|2|3` (New/Learning/Review/Relearning) following the FSRS spec. The dashboard shows different UI phases depending on whether cards are new (state=0) or due for review (state≠0).

**FSRS params** are stored as a JSON string in `settings.fsrs_params` and parsed at runtime.

**Batch import format (BatchAddPage):** One card per line, comma/tab-separated: `word, sentence, note`
