# Flashcard

A self-hosted spaced repetition flashcard app powered by the [FSRS](https://github.com/open-spaced-repetition/fsrs4anki) algorithm.

## Features

- FSRS algorithm for optimal review scheduling
- Daily cap of 20 new cards
- Streak tracking
- Batch import
- Word list editor
- Per-card updates persisted immediately to backend

## Stack

| Layer | Tech |
|---|---|
| Frontend | React 19 + TypeScript + Vite, Mantine 9, TanStack Query, React Router |
| Backend | FastAPI (Python) |
| FSRS | `py-fsrs` for review scheduling |
| Database | SQLite |
| Serving | Nginx |
| Container | Docker Compose |
| Access | Tailscale (or Cloudflare Tunnel) |

## Setup

### Prerequisites

- Docker + Docker Compose
- A Linux machine accessible via Tailscale or Cloudflare Tunnel

### Run

```bash
docker compose up -d
```

The app is served at `http://localhost` (port 80). The SQLite database is persisted at `./data/flashcard.db`.

## Batch Import Format

One card per line, `::` separated:

```
word::note::example sentence
Apple::蘋果::An apple a day keeps the doctor away.
```

## Architecture

```
[Browser]
    |
    | HTTP (Tailscale)
    v
[Nginx :80]
    ├── /        → Vite static build (React SPA)
    └── /api/*   → FastAPI :8000 (internal)
                      └── SQLite (./data/flashcard.db)
```

## API

Nginx maps `/api/*` → backend `/*`.

| Method | Endpoint | Description |
|---|---|---|
| GET    | `/api/health` | Health check |
| GET    | `/api/stats` | `{streak_count, due_count, new_available, ...}` for the dashboard |
| GET    | `/api/cards/queue` | Today's review queue (due + new, capped) |
| GET    | `/api/cards/search?q=...` | Search by word for the Edit page |
| GET    | `/api/cards` | All cards (rare; used by sync) |
| POST   | `/api/cards/batch` | Batch add cards (BatchAdd page) |
| PATCH  | `/api/cards/{id}` | Update a card's sentence / note |
| POST   | `/api/cards/{id}/review` | Submit a rating (1–4) → returns the rescheduled card |
| GET    | `/api/settings` | Fetch settings (runs streak logic) |
| PATCH  | `/api/settings` | Update settings |
| POST   | `/api/sync` | Upsert all cards + settings |

## UI

Follows the [utility repo's design language](../README.md#design-language) — monochrome warm-gray surfaces with one honey accent per page. Review-rating buttons (Again / Hard / Good / Easy) intentionally drop the conventional color coding in favor of position-based muscle memory + keyboard hints `[1] [2] [3] [4]`.

## Work in progress — example-sentence regeneration

**Goal:** the ✨ button on the flipped review card calls a local LLM to generate one example sentence per definition (split by POS markers in `note`), and renders defs + examples aligned in the UI.

**Done (uncommitted):**

- `POST /api/cards/{id}/examples` — backend endpoint. Reads `word` + `note`, prompts `qwen3:8b` via `http://ollama:11434/api/generate`, parses the numbered response, persists to `sentence` as a `\n`-separated numbered list. Returns the updated card.
- `backend/requirements.txt`: added `requests==2.32.3`.
- `docker-compose.yml`: `flashcard-backend` now joins the external `my_network` so it can reach the shared `ollama` container by DNS name.
- `nginx/nginx.conf`: bumped `proxy_read_timeout` / `proxy_send_timeout` to `180s` on `/api/` (ollama calls run 30–120s).
- `src/api.ts`: added `regenerateExamples(id)`.
- `src/pages/ReviewPage.tsx`: replaced clipboard copy button with ✨ `IconSparkles` + `Loader` spinner during the call; sentence `<Text>` now uses `whiteSpace: 'pre-line'` so the numbered lines wrap correctly. Mutation patches the queue head only if it's still the same card (rating mid-generation is safe).
- **Env vars** (backend, optional): `OLLAMA_URL` (default `http://ollama:11434`), `OLLAMA_MODEL` (default `qwen3:8b`).

**Pending — UI interleaving:**

Currently `note` (defs) and `sentence` (examples) render as two separate blocks. Next step is to parse both client-side and render them aligned:

```
n. 股份；赌注；利害关系
   "He has a personal stake in the success of the movie."
vt. 以…打赌，拿…冒險
   "The President staked out his position on the issue."
```

Parsing rules: split `note` on POS markers `n. | v. | vt. | vi. | adj. | adv. | prep. | conj. | pron. | interj.`; split `sentence` on `\n`, strip leading `\d+\.\s*`. Zip by index. If counts mismatch, fall back to the current side-by-side rendering instead of crashing.
