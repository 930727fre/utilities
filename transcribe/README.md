# transcribe

Paste a YouTube URL, wait, download SRT — or stream the video/audio back with captions. GPU-accelerated Whisper transcription via Celery queue.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React in its own container, proxies `/api` and `/player` to the backend |
| Backend API | FastAPI on port 8000 (no GPU) |
| Worker | Celery on the same image, GPU-exclusive (`--concurrency=1 -P solo`) |
| Queue broker | Redis |
| Downloader | `yt-dlp` (best mp4) |
| Transcriber | `openai-whisper` model `medium`, `device=cuda` |
| Storage | `data/jobs.json` (file-locked) + `data/downloads/*.mp4` + `.srt` + inbox `.mp3` |

## Services

```
docker compose
├── transcribe-redis      # Redis broker (no GPU)
├── transcribe-backend    # FastAPI — API + /player route (no GPU)
├── transcribe-frontend   # Vite + React — dashboard, proxies /api & /player (no GPU)
├── transcribe-worker     # Celery worker (GPU)
└── transcribe-beat       # Celery scheduler (cleanup, periodic tasks)
```

GPU reservation lives on `transcribe-worker` only. Models cache to `data/models/` (Whisper) which is bind-mounted so a container rebuild doesn't re-download the ~1.5 GB weights.

## Run

Prereqs:
- NVIDIA Driver 525+ and `nvidia-container-toolkit` installed.
- External Docker network `my_network` if you're fronting with cloudflared.

```sh
docker compose up -d --build
```

First transcription pulls the Whisper `medium` model on first use. Subsequent runs reuse `data/models/`.

## API

The backend exposes these routes. The frontend container's Vite proxy forwards `/api`, `/player`, and `/health` to the backend.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Liveness check used by the frontend |
| `POST` | `/api/jobs` | Submit a new URL → returns `{job_id, status: "PENDING"}` |
| `GET`  | `/api/jobs` | List all jobs |
| `GET`  | `/api/jobs/{id}` | Single job |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed job |
| `DELETE` | `/api/jobs/{id}` | Cancel + remove job and its files |
| `GET`  | `/api/download/{id}/{kind}` | `kind` ∈ `mp4` / `mp3` / `srt`; downloads the file |
| `GET`  | `/player/{id}` | Standalone player page (new tab) |
| `GET`  | `/api/stream/{id}/video` | MP4 with `Range` support for seek |
| `GET`  | `/api/stream/{id}/audio` | MP3 with `Range` support |
| `GET`  | `/api/stream/{id}/subtitle` | SRT → VTT on the fly for the `<track>` element |

## Job states

```
PENDING → DOWNLOADING → TRANSCRIBING → SUCCESS
                                      ↘ FAILED  (any unhandled exception)
```

The dashboard polls `/api/jobs` every 2 s. Each row is collapsed by default — tap to reveal the action row (`▸ play …… SRT …… ✕` for ready jobs; `↻ retry …… ✕` for failed). The currently-working `○` glyph pulses; failed jobs show a static `!`.

UI follows the [utility repo's design language](../README.md#design-language): monochrome warm-gray surfaces, single honey accent (the `→` submit button), character glyphs for status.

## Known limitations

- **Crash recovery is fragile.** If `transcribe-worker` dies mid-job, the job stays in `DOWNLOADING` or `TRANSCRIBING` forever — there's no startup sweep to mark orphaned jobs as `FAILED` like marker-pipeline does. Restart the stack and manually delete the stuck row.
- **`jobs.json` is file-locked**, not a real DB. For two users hammering at once you'd want SQLite — fine for single-user.
- **Whisper model is hardcoded** to `medium`. Larger models would mean better accuracy + much longer GPU time.
