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
| Frontend | React 19 + TypeScript + Vite, Zustand, Mantine 9 |
| Backend | FastAPI (Python) |
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

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Health check |
| `/api/cards` | GET | Fetch all cards |
| `/api/cards/batch` | POST | Batch add cards |
| `/api/cards/:id` | PATCH | Update a card |
| `/api/settings` | GET | Fetch settings (runs streak logic) |
| `/api/settings` | PATCH | Update settings |
| `/api/sync` | POST | Upsert all cards + settings |
