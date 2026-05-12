# TwelveReader

> Web-based EPUB audiobook reader. Upload EPUB → converted to markdown → on-demand TTS via Kokoro-82M → frontend renders with synchronized highlight, auto-scroll, and click-to-seek playback.

---

## 🚧 Planned refactor: swap conversion pipeline to [datalab-to/marker](https://github.com/datalab-to/marker)

The entire Ollama + BeautifulSoup conversion pipeline (`epub_parser.py` and the "Open concerns" list below) will be replaced with marker. Marker handles PDF, image, PPTX, DOCX, XLSX, HTML, and EPUB through a single Surya (OCR/layout) + Texify (equations) pipeline.

**Rationale**: marker is purpose-built for "any document → markdown" and is more capable than the current per-block LLM approach (especially on equations, complex tables, columnar layouts). For EPUB-only workloads its machinery is heavy, but the pipeline cost is justified by (a) gaining PDF and other formats and (b) deleting ~200 lines of brittle parser code plus the Ollama dependency.

**Execution mode**: **one-shot subprocess** (`marker_single`). Models load, convert, exit, VRAM released. Right pattern for the weekly conversion cadence; avoids fighting Ollama for VRAM on the 3060.

**Priority: quality over speed.** Conversion runs are infrequent (once per week-ish), so optimize for output fidelity, not throughput. Concretely: pass `--use_llm` to enable marker's optional LLM-enhancement pass (improves math/table/layout accuracy), accept the longer per-book runtime, and don't bother with model-preloading or warm-worker optimizations. The dumbest "spawn subprocess, wait, ingest" path is the right one.

### Things to remember when the refactor lands

- **Existing books' bookmarks will orphan.** `paragraph_id(book_id, index, text[:100])` depends on paragraph text. Marker's markdown will differ from Ollama's character-for-character, so all cached TTS WAVs and `bookmark_paragraph_index` references will point at the wrong paragraphs. Two options:
  1. *Wipe and re-upload* — simplest. Add a one-line note in the upgrade instructions.
  2. *Re-key paragraph IDs to (book_id, index) only* — keeps bookmarks working through the swap, but loses the content fingerprint that detects out-of-sync caches.
- **`my_network` can be dropped from `docker-compose.yml`.** TwelveReader's only reason to be on it was the Ollama HTTP call. Backup reads from sibling dirs via bind mount, not HTTP. Verify no other service on `my_network` calls into TwelveReader before removing.
- **Add a named volume for marker's model cache.** Surya/Texify weights are several GB; mount `marker-models:/root/.cache/huggingface` so they survive container rebuilds.
- **Frontend `accept` attribute needs widening** — PDF/DOCX/etc. The backend `routers/books.py` also hard-rejects non-`.epub` filenames; that check must be loosened.
- **`/api/books/{id}/epub` route** is EPUB-specific. Rename to `/source` and serve whatever extension was uploaded.
- **`bookshelf.json`** has no `source_format` field. Add one so the frontend can know whether to offer "view source PDF" vs. "view source EPUB" downloads.
- **Files safe to delete during the swap**: `backend/epub_parser.py`, `backend/test_data.txt`, `backend/test_gemini.py`, `backend/bitcoin*`, `backend/routers/paragraphs.py` (empty placeholder).
- **`requirements.txt`** drops `beautifulsoup4` and `httpx`. Adds `marker-pdf[full]` (the `[full]` extras enable non-PDF formats).

The current pipeline documentation below is **kept for reference until the refactor is verified end-to-end** — then this README will be rewritten from scratch.

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
- **BeautifulSoup4** — parses XHTML, extracts block-level elements (`<p>`, `<h1>`–`<h6>`, `<table>`, `<ul>`, `<ol>`, `<blockquote>`, `<pre>`, `<figure>`, `<img>`) in document order
- **Ollama (qwen2.5:14b, temperature=0.2)** — converts each HTML block to markdown. Output validated with a few-shot pure-markdown check (temperature=0). Up to 10 retries per block. Falls back to BeautifulSoup `get_text()` on exception.
- **Kokoro-82M** (`hexgrad/Kokoro-82M`) — TTS, NVIDIA GPU

### Services
- **twelvereader-backend** — FastAPI, GPU, on `default` + `my_network`
- **twelvereader-frontend** — React/Vite, on `default` + `my_network`
- **ollama** — separate compose in `ollama/`, on `my_network`, GPU

---

## EPUB Conversion Pipeline

```
EPUB
 ├─ extract images ──► book_dir/images/*.jpg  (Python, no LLM)
 └─ per chapter XHTML:
      BeautifulSoup → [<p>, <h2>, <table>, <ul>, ...]
            │
            └─ per block:
                  Ollama (T=0.2): HTML block → markdown
                        │
                        └─ validate: pure markdown? (few-shot, T=0)
                              ├─ True  ──► accept
                              └─ False ──► retry up to 10x
                                          └─ fallback: BeautifulSoup get_text()
            │
      '\n\n'.join → chapter markdown
            │
      flush to {book_id}.md  (live, per chapter)
```

- Images extracted to flat `book_dir/images/` — markdown stores `images/filename.jpg`
- Frontend img renderer prepends `/api/books/{id}/assets/` at render time
- Footnote superscript markers stripped by Ollama prompt
- Conversion progress written to `/data/conversion.log`

---

### ⭐ Most important: blocking strategy

The single biggest lever on output quality is **how blocks are batched into Ollama calls**. Current pipeline gets ~95–97% — the remaining gap and almost all hallucinations / dropped instructions trace back to two failure modes:
- **One block too big** — a 100-item `<ul>` or 50-row `<table>` is sent as a single call. The local model (qwen2.5:14b) loses focus past a few hundred chars, starts editorializing, dropping rows, or refusing.
- **One block too small, no neighbors** — a 5-word `<p>` is converted in isolation with no surrounding context. Style drifts across the document and the model has nothing to anchor on.

**Goal: balance block size and context** — give the local model enough surrounding text to see the "big picture", but never so much that obedience breaks down.

**Strategy: pack adjacent blocks within a budget; let neighbors *be* the context.**

```
budget ≈ 800–1200 chars (tune per model)

pack adjacent blocks within the same chapter into a batch up to budget
  → one Ollama call per batch, blocks separated by explicit delimiters
  → split the response on the same delimiters

if a single block exceeds budget:
  table  → row-group chunks, repeat <thead> in each chunk
  list   → split by item count
  long p → split by sentence
  (each chunk converted independently, joined after)

never span chapter boundaries (resets the model's frame)
keep heading + first body block of a section together when possible
  (so the model sees "this is the start of a section" context)
```

**Two knobs to tune empirically against a corpus of real EPUB chapters:**
- **Budget size** — too small = inconsistent style across paragraphs; too big = model editorializes or skips. Sweet spot is whatever your eval shows on tricky chapters (footnotes, mixed lists, inline images).
- **Pack greedy vs. balanced** — greedy (fill until full, then start a new batch) is simplest and usually fine. Balanced (split a 1100-char run into 2×550 instead of 1000+100) only matters if you observe the trailing tiny batch misbehaving.

The chunker is one pure function (`_chunk_blocks(blocks, max_chars) -> list[list[str]]`) sitting between `_extract_blocks` and the Ollama call — no I/O, easy to unit-test against a corpus before wiring into `convert_epub`.

The companion lever is the **prompt**: small models are obedient when given concrete examples and clear output framing. Few-shot examples in the system prompt, an output sentinel (`<<<MD\n...\n>>>`) for deterministic extraction, and negative directives placed last (recency bias). But blocking is the bigger lever — fix it first.

---

### Open concerns

**Conversion pipeline — silent content loss (high)**
- **`split_paragraphs` shreds fenced code blocks** (`epub_parser.py:218`) — splits on `\n{2,}` unconditionally, so any ` ``` ` block with internal blank lines becomes multiple "paragraphs". Each fragment is rendered/TTS'd separately and the code block visually breaks. Same problem hits loose lists and any markdown with intentional double-newlines.
- **`_extract_blocks` drops common block types** (`epub_parser.py:14-21`) — `<dl>`/`<dt>`/`<dd>` (glossaries), `<hr>` (section breaks, common in essay/aphorism books), and `<svg>` (inline diagrams) are not in `_CONTENT_BLOCKS`. `<center>`, `<nav>`, `<details>` are not in `_CONTAINERS`, so children of those wrappers are never visited.
- **`_fallback_convert` is a flattener, not a converter** (`epub_parser.py:161-176`) — when Ollama errors, `<ul>`/`<ol>`/`<table>`/`<blockquote>` fall through to `tag.get_text(separator=' ')`. A 5-row table becomes one space-joined line; a list loses bullets; blockquotes lose `>` markers. Images inside `<p>` are dropped entirely (only standalone `<img>`/`<figure>` are handled).
- **Empty Ollama output silently dropped** (`epub_parser.py:196`) — if the model returns `''` (refusal, all-whitespace, token-filtered), `if md_block:` skips it without invoking the fallback. Block is lost without a log entry.

**Conversion pipeline — robustness (medium)**
- **OPF-relative href join is naive** (`epub_parser.py:60`) — `'/'.join(filter(None, [opf_dir, href]))` doesn't normalize `./` or `../`. EPUBs with `href="../Text/chapter.xhtml"` produce a literal path that never matches any zip entry, and the chapter is silently dropped via the `KeyError` swallow. Use `posixpath.normpath`.
- **Hardcoded UTF-8** (`epub_parser.py:62`) — `decode('utf-8', errors='replace')` mangles older EPUBs in Windows-1252 / ISO-8859-1. Should honor the `<?xml encoding="..."?>` declaration.
- **Image filename collisions** (`epub_parser.py:79`) — `_extract_images` flattens to basename; EPUBs with `chapter1/img.jpg` and `chapter2/img.jpg` end up with one image and references all point to the survivor.
- **`'nav' not in properties` is substring match** (`epub_parser.py:50`) — splits on whitespace would be correct.
- **Case-sensitive extension filter** (`epub_parser.py:58`) — `.HTML` / `.XHTML` chapters are skipped.
- **`html.parser` is HTML5, not XHTML-aware** — self-closing-tag handling differs from a true XML parser. `lxml` xml mode would round-trip XHTML faithfully.

**Ollama prompt / call (medium)**
- **Raw HTML attributes sent to the model** (`epub_parser.py:151-158`) — `class="..."`, `id="..."`, `style="..."`, `epub:type="..."` are visible to Ollama and likely the source of CSS-class leakage in output. Strip attributes (keep only `src`, `alt`, `href`) before serializing the block.
- **`temperature=0.2` for a deterministic task** (`epub_parser.py:111`) — HTML→MD is mechanical; `temperature=0` is the right default. Non-zero is the source of "occasional artifacts / commentary" symptoms.
- **No `max_tokens`, no input length guard** (`epub_parser.py:107-112`) — a pathological `<table>` with thousands of rows blows past `num_ctx=16384`; an unbounded model can also ramble far past the input.
- **Streaming parser is fragile** (`epub_parser.py:118-125`) — `json.loads(line[6:])` raises on any malformed/keep-alive line and torches the whole block. Wrap each line parse, not the whole stream.
- **No retry, no batching** — system prompt re-sent per block; for a 500-block chapter that's 500 sequential round-trips. README previously claimed "up to 10 retries per block" but `convert_epub` has zero retries; the only fallback is on raised exception. Validator (`_is_pure_markdown`) is implemented but bypassed (kept rejecting valid markdown like `# Foreword` — partly because one of its few-shot negative examples, `See Figure 3.right`, is itself valid plain markdown).
- **Colspan tables** — HTML `colspan` has no GFM equivalent. Ollama handles it better than html2text but output varies.

**Conversion pipeline — low**
- **`figcaption` lookup runs `find()` twice** (`epub_parser.py:173`) — use the walrus operator.
- **Alt text not escaped** (`epub_parser.py:174`) — `![{alt}](...)` breaks if `alt` contains `]` or newlines.
- **`total = len(chapters)` is unused** (`epub_parser.py:182`) — dead variable.
- **Conversion log overwritten per upload** (`epub_parser.py:186`) — two concurrent parses race on the same file; the assumption isn't enforced.
- **No conversion checkpoint/resume** — failure mid-book throws away all converted chapters.

**Backend API**
- **Case-sensitive EPUB check** — `routers/books.py` rejects uploads whose filename does not end with lowercase `.epub`. `.EPUB` uploads are rejected; also crashes if `file.filename` is `None`.
- **Markdown reloaded per TTS request** — `routers/tts.py` re-reads and re-splits the full book markdown on every paragraph generate and every evict. For long books this is a lot of repeated I/O. A small in-memory cache keyed by `book_id` (with mtime check) would help.
- **Stale router scaffolding** — `routers/paragraphs.py` is a single-comment placeholder, never imported. `routers/__init__.py` is empty. Safe to delete.
- **Test artifacts in repo** — `backend/test_data.txt`, `backend/test_gemini.py`, `backend/bitcoin.epub`, `backend/bitcoin - 複製*` are leftover scratch files committed alongside production code.

**Frontend**
- **`new Audio()` per render** — `usePlayer.js` does `useRef(new Audio())`, which constructs a new `Audio` on every render and throws all but the first away. Cosmetic waste. Switch to lazy init: `const audioRef = useRef(null); if (!audioRef.current) audioRef.current = new Audio()`.
- **Incomplete effect dep arrays** — `usePlayer.js` bookmark effect omits `bookId`; `Reader.jsx` scroll/bookmark effects omit `virtualizer` and `book.id`. Works in practice (these refs are stable) but should be fixed for lint cleanliness.
- **Dead frontmatter strip** — `Reader.jsx#splitParagraphs` strips YAML frontmatter that the backend never emits. Defensive code with no current source.
- **Empty `frontend/public/audio/`** — the fallback WAV is generated server-side at `/data/static/tts_failed.wav` and proxied via `/audio`. The frontend folder serves no purpose.

**Docker / config**
- **No backend port published** — `docker-compose.yml` exposes the backend only on the docker network (`twelvereader-backend:8000`); host-side debugging requires adding `ports:` or `docker exec`.
- **`npm install` not `npm ci`** — `frontend/Dockerfile` uses `npm install`, which can drift from the lockfile. Use `npm ci` for reproducible builds.

---

## Core Features

### 1. EPUB Reader
- Upload EPUB → backend extracts block elements via BeautifulSoup → Ollama converts each block → saved as `{book_id}.md`
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
2. Images extracted to `/data/{book_id}/images/`
3. Background task: BeautifulSoup → Ollama per block → write `{book_id}.md` (flushed per chapter)
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
    images/                             ← extracted images (flat, served via assets endpoint)
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
