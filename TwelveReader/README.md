# TwelveReader

> Web-based EPUB audiobook reader. Upload EPUB → backend parses paragraphs → on-demand TTS via Kokoro-82M → frontend renders EPUB with synchronized highlight, auto-scroll, and click-to-seek playback.

---

## Overview

| Item | Detail |
|------|--------|
| Repo | `TwelveReader` |
| Deployment | Docker Compose, home PC with NVIDIA GPU |
| Users | Single user, no auth |
| Language | English (MVP) |

---

## Stack

### Frontend
- **React** (Vite dev server)
- **Manual EPUB renderer** — fetches spine items via `/api/books/{id}/item/{path}`, rewrites asset paths, injects `data-paragraph-id` by positional matching

### Backend
- **Python + FastAPI**
- **SQLite** — book metadata, paragraphs, bookmarks
- **Kokoro-82M** (`hexgrad/Kokoro-82M`) — TTS model, NVIDIA RTX 3060 GPU
- **ebooklib + BeautifulSoup** — EPUB parsing

### Deployment
- **Docker Compose** — frontend + backend containers, backend with GPU passthrough
- **nvidia-container-toolkit** — required on host

---

## Core Features

### 1. EPUB Reader
- Manual rendering: spine items fetched from backend, rendered with `dangerouslySetInnerHTML`
- Asset paths (`href`, `src`) rewritten to `/api/books/{id}/item/{path}`
- Prev / next navigation via `spineIndex` state
- Auto-navigates to the spine item containing the current playing paragraph

### 2. Paragraph-level Synchronized Playback
- Current paragraph highlighted with yellow background
- Auto-scrolls to current paragraph (`scrollIntoView`)
- Player auto-advances through all paragraphs continuously

### 3. Click-to-seek
- Click any paragraph → immediately jumps and plays from that paragraph
- Clears TTS cache and rebuilds sliding window from new position

### 4. Bookmark (Resume)
- Saves last played `paragraph_id` per book
- On reopen: scrolls to bookmark position, does not auto-play

### 5. Continuous Playback
- Paragraph ends → automatically loads and plays next
- Auto-navigates spine items as needed
- Reaches last paragraph → stops, state returns to IDLE

---

## Paragraph Parsing

Parsed tags: `<p>`, `<h1>`, `<h2>`, `<h3>`, `<li>`

### Paragraph ID

```python
def make_paragraph_id(book_id, spine_href, tag, text, para_index):
    raw = f"{book_id}|{spine_href}|{tag}|{text[:100]}|{para_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

| Field | Description |
|-------|-------------|
| `book_id` | Book UUID |
| `spine_href` | EPUB spine item href |
| `tag` | Element tag name |
| `text[:100]` | First 100 chars of paragraph text |
| `para_index` | Position within spine item |

Frontend and backend use identical traversal order (`querySelectorAll` / BeautifulSoup `find_all`) for positional matching — no hash recomputation needed in the browser.

---

## TTS: On-demand + Sliding Window Cache

| Item | Detail |
|------|--------|
| Model | `hexgrad/Kokoro-82M` |
| Hardware | NVIDIA RTX 3060 |
| Unit | 1 paragraph = 1 WAV file |
| Format | WAV (via soundfile) |
| Voice | `af_heart` (fixed) |
| Generation | On-demand, sequential (GPU thread safety) |
| Retry | Up to 3 times per paragraph |
| Fallback | Plays `/audio/tts_failed.wav` → continues to next |

### Sliding Window
```
[N-1 kept]  [N playing]  [N+1 prefetch]  [N+2 prefetch]
```

- When paragraph N starts, N+1 and N+2 are prefetched in the background
- When paragraph N starts, N-2 is evicted from disk (keeps at most 4 files per book)
- Entire cache cleared on seek (click-to-seek rebuilds from new position)
- Entire cache cleared on book deletion

---

## Book Upload Flow

```
UPLOADING → PARSING → READY
                    ↘ FAILED
```

1. Upload EPUB → saved to `/data/books/{book_id}/book.epub`
2. Background task parses paragraphs → writes to SQLite
3. Status set to `READY` — no TTS pre-generation needed
4. Frontend polls every 2s until `READY`

---

## File Structure

```
/data/
  books/{book_id}/book.epub
  cache/{book_id}/{paragraph_id}.wav
  static/tts_failed.wav               ← fallback audio, generated on first startup
```

---

## SQLite Schema

```sql
CREATE TABLE books (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    author      TEXT,
    epub_path   TEXT NOT NULL,
    status      TEXT NOT NULL,   -- PARSING | READY | FAILED
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE paragraphs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         TEXT NOT NULL REFERENCES books(id),
    paragraph_id    TEXT NOT NULL,
    spine_href      TEXT NOT NULL,
    para_index      INTEGER NOT NULL,
    tag             TEXT NOT NULL,
    text            TEXT NOT NULL,
    UNIQUE(book_id, paragraph_id)
);

CREATE TABLE bookmarks (
    book_id         TEXT PRIMARY KEY REFERENCES books(id),
    paragraph_id    TEXT NOT NULL,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## API

### Books
```
POST   /api/books                        Upload EPUB, parse paragraphs
GET    /api/books                        List all books
GET    /api/books/{id}                   Book detail + status
DELETE /api/books/{id}                   Delete book, paragraphs, cache
GET    /api/books/{id}/spine             Ordered spine items [{index, href}]
GET    /api/books/{id}/paragraphs        All paragraphs in order
GET    /api/books/{id}/item/{path}       Raw EPUB asset (HTML, CSS, images)
GET    /api/books/{id}/epub              Raw EPUB file
```

### TTS
```
POST   /api/tts/{book_id}/{paragraph_id}   Generate or return cached WAV
DELETE /api/tts/{book_id}/{paragraph_id}   Evict single cached WAV
DELETE /api/tts/{book_id}/cache            Clear entire book TTS cache
```

### Bookmarks
```
GET  /api/books/{id}/bookmark   Get last position
PUT  /api/books/{id}/bookmark   Save position { paragraph_id }
```

---

## Player State Machine

```
IDLE
  │ click play / click paragraph
  ▼
GENERATING  (POST /api/tts/...)
  │ success              │ failure (after 3 retries)
  ▼                      ▼
PLAYING            PLAYING_FALLBACK
  │ audio ended          │ fallback ends
  ▼                      ▼
next paragraph →  GENERATING     IDLE
  │ no next paragraph
  ▼
IDLE
```

---

## Docker Compose

```yaml
services:
  backend:
    build: ./backend
    volumes:
      - ./data:/data
      - ./data/static:/app/static
    ports:
      - "8000:8000"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      - BACKEND_URL=http://backend:8000
```

### Host requirements
```bash
# Install nvidia-container-toolkit and configure Docker runtime
sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Reset data
```bash
# Wipe DB, books, and TTS cache (bind mounts — not affected by down -v)
docker compose down
rm -rf ./data
docker compose up --build
```

### Cross-device access (Tailscale etc.)
All API calls use relative URLs and are proxied through the Vite dev server on port 3000. Only port 3000 needs to be reachable from other devices — port 8000 is internal.

**Do not set `VITE_API_URL`** — if set, the browser resolves it as an absolute URL (e.g. `http://localhost:8000`) which points to the client device's own localhost, breaking all API calls from non-host devices.

---

## Non-Goals (MVP)

- Offline / PWA
- User login / auth
- Sentence or word-level alignment
- PDF support
- Multi-language TTS
- Speed / voice controls
- Pre-transcoding entire book
