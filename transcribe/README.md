# transcribe

Two ways in:

- **YouTube tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **Library tab** — see every video sitting in qBittorrent's downloads (`/qb`) and the transcribed library (`/app/data/downloads`); click `▸` on a file with no SRT to run Whisper in place.

In both cases, Claude annotation (`✨ sparkle`) runs automatically once a transcript exists. A background loop also catches externally-arriving SRTs (torrent-bundled subs, manual drops) and annotates them too. Cost per ~1-hour SRT is around $0.05.

`data/downloads/` is mounted into [jellyfin](../jellyfin) as `/transcribed` (read-only); `/qb` is the same folder qBittorrent writes to, mounted by both services.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React — tab between YouTube jobs and Library file view |
| Backend | FastAPI on port 8000 — API + in-process workers on a single GPU |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes whisper onto the GPU |
| Whisper isolation | each transcription runs in a `multiprocessing.spawn` subprocess so VRAM is released between jobs |
| Downloader | `yt-dlp` (best mp4 ≤ 1080p) |
| Transcriber | `openai-whisper` model `medium`, `device=cuda` |
| Annotator | Claude (sonnet) via tool-use, chunked by cue count |
| Storage | `data/jobs.json` (file-locked) + on-disk video + sidecar SRT |

## On-disk layout

```
data/downloads/                  ← YouTube output
  <sanitized title>.mp4
  <sanitized title>.srt          ← annotated in place

/qb/<torrent-folder>/            ← qBittorrent downloads (host bind)
  Show.S01E01.mkv
  Show.S01E01.srt                ← written by Library transcribe, or torrent-bundled
```

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Jellyfin auto-loads any `.srt` as a sidecar.

## Run

Prereqs:
- NVIDIA Driver 525+ and `nvidia-container-toolkit` installed.
- External Docker network `my_network`.
- Sibling `qbittorrent/` directory exists (the compose bind-mounts `../qbittorrent/data/downloads`).
- qBittorrent's "Append .!qB extension to incomplete files" enabled (so the Library scan skips in-flight downloads).
- `ANTHROPIC_API_KEY` exported in the shell — required for annotation. Compose's `${VAR:?err}` fails fast at parse time if missing.

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
| `GET`  | `/api/jobs?source=youtube\|library` | List jobs in one bucket; default `youtube` |
| `GET`  | `/api/jobs/{id}` | Single job |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed job |
| `DELETE` | `/api/jobs/{id}` | Mark deleted; YouTube jobs also remove their mp4 + srt |
| `GET`  | `/api/library` | Scan `/qb` + `/app/data/downloads`; return file + annotation state |
| `POST` | `/api/library/transcribe` | `{path}`: run Whisper on a library file; auto-chains annotation |

## Job states

```
YouTube:  PENDING → DOWNLOADING → TRANSCRIBING → ANNOTATING → SUCCESS
                                                ↘ FAILED
Library:  PENDING → TRANSCRIBING → ANNOTATING → SUCCESS
                                  ↘ FAILED      (annotation-only jobs skip TRANSCRIBING)
```

Crashed `PENDING` / `DOWNLOADING` / `TRANSCRIBING` jobs flip to `FAILED` on startup. `ANNOTATING` crashes flip to `SUCCESS` with `annotation_error` set; for library jobs the background loop will retry.

## Annotation

Claude scans the SRT for U.S.-cultural references a Taiwanese viewer might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues. The annotated SRT overwrites the original; the `※` marker is the persistent on-disk signal that a file has been annotated.

A background loop scans the library every 30s for SRTs without `※` and queues them. Per-path failure counter caps at 3 attempts (in-memory; container restart resets it).

## Library scan filters

The Library tab and background annotation loop skip:

- Files ending in `.!qB` (qBittorrent's incomplete-file marker)
- Any path with an `incomplete/` component
- Dotfiles
- Files whose mtime is within the last 60 seconds (still being written)
- UUID-named staging files belonging to in-flight YouTube jobs

Video extensions recognized: `.mp4 .mkv .avi .mov .ts .webm`.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is hardcoded to `medium`. Larger = better accuracy + much longer GPU time.
- Library mode ignores embedded subtitle tracks (muxed into the video container) and non-standard sub layouts like `Subs/<episode>/2_English.srt` — only same-stem sidecar `.srt` files are recognized.
