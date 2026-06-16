# transcribe

Paste a YouTube URL → get an mp4 + Whisper-generated SRT named after the video, dropped into `data/downloads/`. Optional Claude-powered annotation embeds 繁體中文 cultural-context notes into the SRT.

The output directory is mounted into [jellyfin](../jellyfin) (`../transcribe/data/downloads:/transcribed:ro`), so anything you transcribe shows up in the media library automatically.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React — submit URL, watch pipeline status, retry/delete/✨ |
| Backend | FastAPI on port 8000 — API + in-process workers on a single GPU |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes whisper onto the GPU |
| Whisper isolation | each transcription runs in a `multiprocessing.spawn` subprocess so VRAM is released between jobs |
| Downloader | `yt-dlp` (best mp4 ≤ 1080p) |
| Transcriber | `openai-whisper` model `medium`, `device=cuda` |
| Annotator | Claude (sonnet) via tool-use, chunked by cue count |
| Storage | `data/jobs.json` (file-locked) + `data/downloads/<title>.{mp4,srt}` |

## On-disk layout

```
data/downloads/
  <sanitized title>.mp4    ← yt-dlp output, renamed after successful transcription
  <sanitized title>.srt    ← whisper output; ✨ overwrites in-place if annotated
```

Filename collisions get `(2)`, `(3)`, … suffixes. Title is sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Jellyfin auto-loads the `.srt` as sidecar.

## Run

Prereqs:
- NVIDIA Driver 525+ and `nvidia-container-toolkit` installed.
- External Docker network `my_network`.
- `ANTHROPIC_API_KEY` exported in the shell — required for ✨ annotation. Compose's `${VAR:?err}` fails fast at parse time if missing.

```sh
export ANTHROPIC_API_KEY=…
docker compose up -d --build
```

First transcription pulls the Whisper `medium` model. Subsequent runs reuse `data/models/`.

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Liveness |
| `POST` | `/api/jobs` | Submit a YouTube URL (single video or `/playlist?list=…`) |
| `GET`  | `/api/jobs` | List all non-deleted jobs |
| `GET`  | `/api/jobs/{id}` | Single job |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed job |
| `POST` | `/api/jobs/{id}/annotate` | Embed 繁中 cultural-context notes into the SRT |
| `DELETE` | `/api/jobs/{id}` | Mark deleted; remove mp4 + srt from disk |

## Job states

```
PENDING → DOWNLOADING → TRANSCRIBING → SUCCESS ⇄ ANNOTATING
                                      ↘ FAILED  (any unhandled exception)
```

Crashed `PENDING` / `DOWNLOADING` / `TRANSCRIBING` jobs flip to `FAILED` on startup (`error = "Interrupted by restart"`). `ANNOTATING` crashes flip back to `SUCCESS` with `annotation_error` set — re-run ✨ from the UI.

## ✨ Annotation

Calls Claude via tool-use to scan the SRT for U.S.-cultural references a Taiwanese viewer might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues.

**The annotated SRT overwrites the original** (single file on disk). To start over, delete the job and resubmit the URL — the plain transcript will be regenerated.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is hardcoded to `medium`. Larger = better accuracy + much longer GPU time.
