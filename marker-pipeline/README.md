# marker-pipeline

> Drag a PDF or EPUB onto the page. Wait. Download a zip with clean markdown, extracted images, and metadata — ready to drop into Obsidian, feed to an LLM, or archive.

Powered by [datalab-to/marker](https://github.com/datalab-to/marker). Runs locally on an NVIDIA GPU; no cloud, no API keys.

---

## What it does

```
upload file               ─►  save under /data/{book_id}/
extract metadata          ─►  title + author from PDF /Title or EPUB OPF
queue conversion          ─►  marker_single runs (GPU-serialized, one at a time)
flatten output            ─►  md + meta.json + images all live in the same folder
scrub empty <span id="…"> ─►  HTML-naive viewers would otherwise show them as raw tags
mark READY                ─►  download button appears
zip on demand             ─►  whole folder streamed back as {title}.zip
```

Per-upload runtime is minutes (marker is OCR-heavy). Multiple files can be uploaded back-to-back without crashing the GPU — they queue.

---

## Stack

| Layer | Tech |
|------|------|
| Frontend | React + Vite, single page (upload list, status, download/delete) |
| Backend | FastAPI + uvicorn |
| Converter | `marker_single` subprocess, serialized with `threading.Lock` |
| Metadata | `pypdf` for PDF, stdlib `zipfile + xml.etree` for EPUB |
| Storage | `bookshelf.json` index + per-file folder under `/data/` |
| Models cache | named volume `model-cache` mounted at `/root/.cache` |
| Deployment | Docker Compose, single host with NVIDIA GPU |
| Public reachability | Cloudflare Tunnel via shared `my_network` |

---

## Per-file layout on disk

```
/data/
├── bookshelf.json
└── {book_id}/
    ├── {book_id}.pdf          ← original upload (or .epub)
    ├── {book_id}.md           ← converted markdown
    ├── meta.json              ← marker's TOC + page stats
    └── _page_*.jpeg / .png    ← extracted figures, flat
```

The downloaded zip is the entire `{book_id}/` folder — source, markdown, metadata, images. Filename is the sanitized book title with `.zip` appended; falls back to the book_id if the title is empty.

---

## Markdown post-processing

Exactly one rule:

| Pattern | Example | Why removed |
|---------|---------|-------------|
| Empty span anchor | `<span id="page-5-0"></span>` | renders as literal escaped tags in HTML-naive viewers |

Everything else marker emits is preserved as-is. In particular, footnote-style links like `[1](#page-7-0)` are **kept** — they're useful visual markers for human readers, and HTML-aware viewers (Obsidian, GitHub, VS Code preview) can navigate them.

---

## Concurrency

Two simultaneous uploads → both immediately move to `PARSING` status, but only one `marker_single` subprocess runs at a time. The lock guarantees VRAM stays bounded (~5 GB while a conversion is active, idle otherwise). Without it, two concurrent runs share the 12 GB on a 3060; three runs OOM.

No external queue (Celery, Redis, etc.) — for single-user weekly use, a process-wide `threading.Lock` is the right amount of machinery.

---

## Run

Prereqs:
- Docker with NVIDIA GPU support.
- External Docker network `my_network` (also used by the Cloudflare tunnel that fronts this).

```sh
docker compose up -d --build
```

First conversion downloads marker's Surya/Texify weights (~5 GB) into the `model-cache` named volume. Subsequent runs reuse them.

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST   | `/api/books` (multipart `file`) | upload a PDF or EPUB; returns `{book_id, status: "PARSING"}` |
| GET    | `/api/books` | list all books |
| GET    | `/api/books/{id}` | single book record |
| GET    | `/api/books/{id}/zip` | streaming zip of the book folder (only when `READY`) |
| GET    | `/api/books/{id}/source` | original uploaded file with the right Content-Type |
| DELETE | `/api/books/{id}` | remove bookshelf entry and on-disk folder |
| GET    | `/health` | liveness check |

Book record schema:

```json
{
  "id": "a3f1c8e2b4d6f8a0",
  "title": "The Little Book of Common Sense Investing",
  "author": "John C. Bogle",
  "status": "READY",
  "source_format": ".pdf",
  "created_at": "2026-05-12T09:05:22.710469+00:00"
}
```

---

## Non-goals

- Multi-user / auth (Cloudflare Access in front handles this for personal use)
- In-app markdown viewer — download the zip and open in your preferred tool
- Background re-conversion / retries — failed conversions surface as `FAILED`; user re-uploads
- Distributed / multi-node workers — single GPU host is the deployment target
