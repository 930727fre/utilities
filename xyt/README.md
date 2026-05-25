# xyt

Paste a YouTube URL, wait, download SRT — or stream the video back with captions. GPU-accelerated Whisper transcription, single-process.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React in its own container, proxies `/api` and `/player` to the backend |
| Backend | FastAPI on port 8000 — API, `/player` route, and in-process worker on a single GPU |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes jobs onto the GPU |
| Whisper isolation | each transcription runs in a `multiprocessing.spawn` subprocess so VRAM is released between jobs |
| Downloader | `yt-dlp` (best mp4) |
| Transcriber | `openai-whisper` model `medium`, `device=cuda` |
| Romanizer | `pykakasi` (Japanese kanji+kana → Hepburn romaji), deterministic, CPU |
| Translator | Ollama HTTP at `http://ollama:11434`, model `qwen3:8b` (configurable via `OLLAMA_MODEL`); one call per cue |
| Storage | `data/jobs.json` (file-locked) + `data/downloads/{id}.mp4` + `.srt`. The `.srt` is a single file where each cue body is 3 stacked lines: original / romaji / zh-Hant. |

## Services

```
docker compose
├── xyt-app        # FastAPI + executor (GPU)
└── xyt-frontend   # Vite + React — dashboard, proxies /api & /player
```

Models cache to `data/models/` (Whisper) which is bind-mounted so a container rebuild doesn't re-download the ~1.5 GB weights.

## Crash recovery

The backend's FastAPI `lifespan` runs a one-pass startup sweep: any job in `PENDING` / `DOWNLOADING` / `TRANSCRIBING` / `ENRICHING` is flipped to `FAILED` with `error = "Interrupted by restart"`. The dashboard surfaces those rows with `!` and the `↻` retry button — user clicks to re-queue. Same shape as marker-pipeline; nothing auto-resumes.

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
| `GET`  | `/api/download/{id}/{kind}` | `kind` ∈ `mp4` / `srt`; downloads the file |
| `GET`  | `/player/{id}` | Standalone player page (new tab) |
| `GET`  | `/api/stream/{id}/video` | MP4 with `Range` support for seek |
| `GET`  | `/api/stream/{id}/subtitle` | SRT → VTT on the fly for the `<track>` element |

## Job states

```
PENDING → DOWNLOADING → TRANSCRIBING → ENRICHING → SUCCESS
                                                  ↘ FAILED  (any unhandled exception)
```

(MP4-only jobs — submit toggle set to `MP4` — skip `TRANSCRIBING` and `ENRICHING` entirely: `DOWNLOADING → SUCCESS`.)

The dashboard polls `/api/jobs` every 2 s. Each row is collapsed by default — tap to reveal the action row (`▸ play …… SRT …… ✕` for ready SRT jobs; `↻ retry …… ✕` for failed). The currently-working `○` glyph pulses; failed jobs show a static `!`.

The standalone player (`/player/{id}`) uses the native `<video>` + `<track>` element pointing at `/api/stream/{id}/subtitle`. The browser renders the 3 stacked lines per cue as native captions at the bottom of the video, including in fullscreen.

UI follows the [utility repo's design language](../README.md#design-language): monochrome warm-gray surfaces, single honey accent (the `→` submit button), character glyphs for status.

## Known limitations

- **`jobs.json` is file-locked**, not a real DB. For two users hammering at once you'd want SQLite — fine for single-user.
- **Whisper model is hardcoded** to `medium`. Larger models would mean better accuracy + much longer GPU time.
- **Translation assumes Japanese source.** Romanization uses pykakasi (Hepburn); translation prompt is hardcoded to JA→zh-Hant. Other source languages will produce garbage in both extra tracks.
- **Translation is one ollama call per cue.** Simple but slow — a 30 min video with ~500 cues takes 10–25 min of GPU time on `qwen3:8b`. Batching cues would help but isn't implemented.
- **Retry re-runs from scratch.** If enrichment fails after Whisper succeeded, retry re-downloads and re-transcribes too. Acceptable for now; can add an enrichment-only retry path later.
- **No persistent queue.** Submitting jobs while the app is down is not possible (no broker absorbs them). For single-user this is fine.
