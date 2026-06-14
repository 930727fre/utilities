# transcribe

Paste a YouTube URL, wait, download SRT — or stream the video/audio back with captions. GPU-accelerated Whisper transcription, single-process.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React in its own container, proxies `/api` and `/player` to the backend |
| Backend | FastAPI on port 8000 — API, `/player` route, and in-process worker on a single GPU |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes jobs onto the GPU |
| Whisper isolation | each transcription runs in a `multiprocessing.spawn` subprocess so VRAM is released between jobs |
| Downloader | `yt-dlp` (best mp4) |
| Transcriber | `openai-whisper` model `medium`, `device=cuda` |
| Inbox scanner | asyncio task in FastAPI lifespan, polls `/app/data/inbox/*.mp3` every 5 s |
| Storage | `data/jobs.json` (file-locked) + `data/downloads/*.mp4` + `.srt` + inbox `.mp3` |

## Services

```
docker compose
├── transcribe-app        # FastAPI + executor + inbox scan (GPU)
└── transcribe-frontend   # Vite + React — dashboard, proxies /api & /player
```

Models cache to `data/models/` (Whisper) which is bind-mounted so a container rebuild doesn't re-download the ~1.5 GB weights.

## Crash recovery

The backend's FastAPI `lifespan` runs a one-pass startup sweep: any job in `PENDING` / `DOWNLOADING` / `TRANSCRIBING` is flipped to `FAILED` with `error = "Interrupted by restart"`. The dashboard surfaces those rows with `!` and the `↻` retry button — user clicks to re-queue. Same shape as marker-pipeline; nothing auto-resumes.

## Run

Prereqs:
- NVIDIA Driver 525+ and `nvidia-container-toolkit` installed.
- External Docker network `my_network` if you're fronting with cloudflared.

```sh
docker compose up -d --build
```

First transcription pulls the Whisper `medium` model on first use. Subsequent runs reuse `data/models/`.

`ANTHROPIC_API_KEY` must be exported before `docker compose up` — used by the ✨ annotation feature. Compose's `${VAR:?err}` syntax fails fast at parse time if it's missing.

```sh
export ANTHROPIC_API_KEY=…
docker compose up -d --build
```

## API

The backend exposes these routes. The frontend container's Vite proxy forwards `/api`, `/player`, and `/health` to the backend.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Liveness check used by the frontend |
| `POST` | `/api/jobs` | Submit a new URL → returns `{job_id, status: "PENDING"}` |
| `GET`  | `/api/jobs` | List all jobs |
| `GET`  | `/api/jobs/{id}` | Single job |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed job |
| `POST` | `/api/jobs/{id}/annotate` | Embed 繁中 cultural-context notes into the SRT (Claude) |
| `DELETE` | `/api/jobs/{id}` | Cancel + remove job and its files |
| `GET`  | `/api/download/{id}/{kind}` | `kind` ∈ `mp4` / `mp3` / `srt`; downloads the file |
| `GET`  | `/player/{id}` | Standalone player page (new tab) |
| `GET`  | `/api/stream/{id}/video` | MP4 with `Range` support for seek |
| `GET`  | `/api/stream/{id}/audio` | MP3 with `Range` support |
| `GET`  | `/api/stream/{id}/subtitle` | Original SRT → VTT on the fly for the `<track>` element |
| `GET`  | `/api/stream/{id}/subtitle_annotated` | Annotated SRT → VTT (404 if ✨ hasn't run) |

## Job states

```
PENDING → DOWNLOADING → TRANSCRIBING → SUCCESS ⇄ ANNOTATING
                                      ↘ FAILED  (any unhandled exception)
```

`ANNOTATING` is entered from `SUCCESS` via ✨ and always returns to `SUCCESS` (with `annotated: true` on success or `annotation_error` set on failure — never `FAILED`, since retry would re-download/re-transcribe).

The dashboard polls `/api/jobs` every 2 s. Each row is collapsed by default — tap to reveal the action row (`▸ ✨ …… SRT …… ✕` for ready jobs; `↻ …… ✕` for failed). Working states pulse the status glyph: `○` for download/transcribe, `✨` for annotate. While `ANNOTATING`, the other icons are disabled so the SRT isn't read/zipped mid-rewrite.

### ✨ Annotation

Calls Claude (`claude-sonnet-4-6`) via tool-use to scan the SRT for U.S.-cultural references a Taiwanese listener might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues.

Output goes to a sibling `<job_id>.annotated.srt`; the original `<job_id>.srt` is **not** modified. The player picks annotated by default when it exists (video tab via `<track>` switcher, audio tab via a toggle button); ZIP download bundles both. `jobs.json` tracks the annotated file at `files.srt_annotated` and sets `annotated: true`.

To re-run ✨ on the same job (e.g. after tightening the prompt): edit `data/jobs.json` and flip `"annotated": true` back to `false` for that job. Optionally delete `data/downloads/<job_id>.annotated.srt` if you don't want the old version preserved. ✨ reappears in the dashboard within the 2 s poll.

UI follows the [utility repo's design language](../README.md#design-language): monochrome warm-gray surfaces, single honey accent (the `→` submit button), character glyphs for status.

## Known limitations

- **`jobs.json` is file-locked**, not a real DB. For two users hammering at once you'd want SQLite — fine for single-user.
- **Whisper model is hardcoded** to `medium`. Larger models would mean better accuracy + much longer GPU time.
- **No persistent queue.** Submitting jobs while the app is down is not possible (no broker absorbs them). For single-user this is fine; for anything async, drop files into `data/inbox/` and the scanner will pick them up on next tick.
