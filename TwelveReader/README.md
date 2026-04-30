# TwelveReader

> Web-based EPUB audiobook reader. Upload EPUB → converted to markdown → on-demand TTS via Kokoro-82M → frontend renders with synchronized highlight, auto-scroll, and click-to-seek playback.

---

## Overview

| Item | Detail |
|------|--------|
| Deployment | Docker Compose, home PC with NVIDIA GPU (RTX 3060 12GB) |
| Users | Single user, no auth |
| Language | English |

---

## Stack

### Frontend
- **React** (Vite, served via `vite preview`)
- **@tanstack/react-virtual** — windowed rendering (~20–30 DOM nodes regardless of book length)
- **react-markdown** + **remark-gfm** — per-paragraph MD rendering with tables, bold/italic/headings
- **BACKEND_URL** env var consumed by vite.config.js at server runtime to configure the `/api`, `/cache`, `/audio`, `/health` proxy

### Backend
- **Python + FastAPI**
- **bookshelf.json** — book metadata + bookmarks
- **html2text** — EPUB HTML → rough markdown (fast, deterministic)
- **Ollama (qwen2.5:14b, temperature=0)** — markdown linter pass: fixes syntax errors, malformed tables, HTML artifacts. Runs per 3-paragraph chunk. Falls back to raw html2text output on error.
- **Kokoro-82M** (`hexgrad/Kokoro-82M`) — TTS, NVIDIA GPU

### Services
- **twelvereader-backend** — FastAPI, GPU, on `default` + `my_network`
- **twelvereader-frontend** — React/Vite, on `default` + `my_network`
- **ollama** — separate compose in `ollama/`, on `my_network`, GPU

---

## EPUB Conversion Pipeline

```
EPUB HTML
  └─ html2text ──► rough markdown
                      └─ Ollama (qwen2.5:14b, T=0, 3 paragraphs/chunk)
                            ├─ success ──► clean markdown
                            └─ error   ──► rough markdown (html2text output)
```

- EPUB assets (images) extracted to `book_dir` and served via `/api/books/{id}/assets/`
- Image paths in markdown rewritten to `/api/books/{id}/assets/{path}`
- Conversion progress streamed to `/data/conversion.log` (host: `TwelveReader/data/conversion.log`)

### Open concerns
- **Ollama table fix quality** — `qwen2.5:14b` at `temperature=0` with "markdown linter" prompt. Works on short chapters (foreword, prologue). Larger chapters still under test — may still go off-script despite small chunk size. If so, **Gemini fallback** (html2text → Gemini) is the next step.
- **Malformed colspan tables** — `html2text` can't represent HTML `colspan` in GFM. Results in mismatched column counts. Ollama pass may or may not fix these. Gemini handles them correctly.
- **Table splitting** — `split_paragraphs` splits on `\n\n`. Tables without blank lines between rows stay intact. Tables with blank lines between rows will be split into non-renderable fragments.

---

## Core Features

### 1. EPUB Reader
- Upload EPUB → backend extracts chapters from OPF manifest → html2text + Ollama linter → saved as `{book_id}.md`
- Frontend fetches full MD, splits on `\n\n`, renders with TanStack Virtual + ReactMarkdown

### 2. Paragraph-level Synchronized Playback
- Current paragraph highlighted in yellow
- Auto-scrolls to current paragraph
- Player auto-advances continuously through entire book

### 3. Click-to-seek
- Click any paragraph → immediately jumps and plays from that position
- Clears TTS cache and rebuilds sliding window from new position

### 4. Bookmark (Resume)
- Saves last played paragraph index per book
- On reopen: scrolls to bookmark position, does not auto-play

### 5. Continuous Playback
- Paragraph ends → automatically loads and plays next
- Reaches last paragraph → stops, returns to IDLE

---

## Paragraph Identity

Paragraphs are derived at runtime by splitting the book's markdown on `\n\n`. Identity is index-based:

```python
def paragraph_id(book_id, index, text):
    raw = f"{book_id}|{index}|{text[:100]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

Used only as a server-side cache key for WAV files. Frontend tracks paragraphs by array index.

---

## TTS: On-demand + Sliding Window Cache

| Item | Detail |
|------|--------|
| Model | `hexgrad/Kokoro-82M` |
| Hardware | NVIDIA RTX 3060 12GB |
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
- When paragraph N starts, N-2 is evicted from disk (max ~4 files per book at a time)
- Entire cache cleared on seek

---

## Book Upload Flow

```
POST /api/books → PARSING → READY
                           ↘ FAILED
```

1. EPUB saved to `/data/{book_id}/{book_id}.epub`
2. EPUB contents extracted to `/data/{book_id}/` (images served as assets)
3. Background task: html2text → Ollama linter per 3-paragraph chunk → write `{book_id}.md`
4. Status set to `READY`
5. Frontend polls every 2s until `READY`

---

## File Structure

```
/data/
  bookshelf.json                        ← all book metadata + bookmarks
  conversion.log                        ← live conversion progress (overwritten per upload)
  {book_id}/
    {book_id}.epub                      ← original EPUB
    {book_id}.md                        ← converted markdown
    OEBPS/ (or similar)                 ← extracted EPUB assets (images etc.)
  cache/{book_id}/{paragraph_id}.wav   ← TTS cache
  static/tts_failed.wav                ← fallback audio, generated on startup
```

---

## API

### Books
```
POST   /api/books                      Upload EPUB
GET    /api/books                      List all books
GET    /api/books/{id}                 Book detail + status
GET    /api/books/{id}/md              Full book markdown
GET    /api/books/{id}/assets/{path}   Serve extracted EPUB asset (images etc.)
GET    /api/books/{id}/epub            Raw EPUB file
DELETE /api/books/{id}                 Delete book + cache
```

### TTS
```
POST   /api/tts/{book_id}/{index}  Generate or return cached WAV for paragraph index
DELETE /api/tts/{book_id}/{index}  Evict single cached WAV
DELETE /api/tts/{book_id}/cache    Clear entire book TTS cache
```

### Bookmarks
```
GET  /api/books/{id}/bookmark   Get last position { paragraph_index }
PUT  /api/books/{id}/bookmark   Save position { paragraph_index }
```

---

## Player State Machine

```
IDLE
  │ play / click paragraph
  ▼
GENERATING  (POST /api/tts/...)
  │ success              │ failure (after 3 retries)
  ▼                      ▼
PLAYING            PLAYING_FALLBACK
  │ audio ended          │ fallback ends
  ▼                      ▼
next → GENERATING        IDLE
  │ no next
  ▼
IDLE
```

---

## Docker Compose

### Ollama (start first, pull model once)
```bash
cd ollama
docker compose up -d
docker exec -it ollama ollama pull qwen2.5:14b
```

### TwelveReader
```powershell
cd TwelveReader
docker compose up -d --build
```

### Host requirements
```bash
sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Reset data
```powershell
docker compose down
rm -rf ./data
docker compose up -d --build
```

---

## Non-Goals

- Offline / PWA
- User login / auth
- Multi-language TTS
- Speed / voice controls
- PDF support
