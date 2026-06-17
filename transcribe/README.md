# transcribe

Two ways in:

- **yt tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **qb tab** — view-only: every video in qBittorrent's downloads (`/qb`). No buttons; a background loop handles everything automatically — videos without SRT get Whisper'd, SRTs without `※` get annotated.

In both cases, Claude annotation runs automatically once a transcript exists. The background loop also catches externally-arriving SRTs (torrent-bundled subs, manual drops). Cost per ~1-hour SRT is around $0.05.

`data/downloads/` is mounted into [jellyfin](../jellyfin) as `/transcribed` (read-only); `/qb` is the same folder qBittorrent writes to, mounted by both services.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React — tab between yt (URL submit) and qb (file browser) |
| Backend | FastAPI on port 8000 — API + in-process orchestrator |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes our per-job state mutations |
| Downloader | `yt-dlp` (best mp4 ≤ 1080p) |
| Transcriber | HTTP POST to the shared [whisper](../whisper) service (`faster-whisper-large-v3-turbo`) |
| Annotator | Claude (sonnet) via tool-use, chunked by cue count |
| SRT matcher | Claude (haiku) via tool-use — rescues bundled `.srt` files that strict same-stem matching misses |
| Storage | `data/jobs.json` (file-locked) + on-disk video + sidecar SRT |

## On-disk layout

```
data/downloads/                  ← YouTube output
  <sanitized title>.mp4
  <sanitized title>.srt          ← annotated in place

/qb/<torrent-folder>/            ← qBittorrent downloads (host bind)
  Show.S01E01.mkv
  Show.S01E01.srt                ← written by qb transcribe, or torrent-bundled
```

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Jellyfin auto-loads any `.srt` as a sidecar.

## Run

Prereqs:
- External Docker network `my_network`.
- The shared [whisper](../whisper) service must be running first — startup health-checks it and crashes if unreachable.
- Sibling `qbittorrent/` directory exists (the compose bind-mounts `../qbittorrent/data/downloads`).
- qBittorrent's "Append .!qB extension to incomplete files" enabled (so the qb scan skips in-flight downloads).
- `ANTHROPIC_API_KEY` exported in the shell — required for annotation. Compose's `${VAR:?err}` fails fast at parse time if missing.

```sh
export ANTHROPIC_API_KEY=…
(cd ../whisper && docker compose up -d)   # start shared whisper first
docker compose up -d --build
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/health` | Liveness |
| `POST` | `/api/jobs` | Submit a YouTube URL (single video or `/playlist?list=…`) |
| `GET`  | `/api/jobs?source=yt\|qb` | List jobs in one bucket; default `yt` |
| `GET`  | `/api/jobs/{id}` | Single job |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed job |
| `DELETE` | `/api/jobs/{id}` | Mark deleted; yt jobs also remove their mp4 + srt |
| `GET`  | `/api/qb` | Scan `/qb`; return file + annotation state |
| `POST` | `/api/qb/transcribe` | `{path}`: manually trigger whisper on a qb file (background loop already handles this; useful for power/curl override) |

## Job states

```
yt:  PENDING → DOWNLOADING → TRANSCRIBING → ANNOTATING → SUCCESS
                                           ↘ FAILED
qb:  PENDING → TRANSCRIBING → ANNOTATING → SUCCESS
                             ↘ FAILED      (annotation-only jobs skip TRANSCRIBING)
```

Crashed `PENDING` / `DOWNLOADING` / `TRANSCRIBING` jobs flip to `FAILED` on startup. `ANNOTATING` crashes flip to `SUCCESS` with `annotation_error` set; for qb jobs the background loop will retry.

## Annotation

Claude scans the SRT for U.S.-cultural references a Taiwanese viewer might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues. The annotated SRT overwrites the original; the `※` marker is the persistent on-disk signal that a file has been annotated.

A background loop scans `/qb` every 30s for both whisper work (video without SRT) and annotation work (SRT without `※`), and queues each through the appropriate executor. Per-path failure counters (whisper / annotation tracked separately) cap at 3 attempts each (in-memory; container restart resets them).

**Before queuing whisper, an LLM rescue step kicks in**: if there's at least one `.srt` in the same folder that strict same-stem matching missed (e.g. release-bundled `Movie.2024.en.srt` next to `Movie.2024.mkv`), Claude Haiku looks at the folder listing and decides whether any sibling `.srt` is actually this video's subtitle. If so, the SRT gets renamed to match the video stem and annotation proceeds normally — saving 10-30 minutes of GPU per rescued file. If no match (or API fails), whisper runs as usual.

## qb scan filters

The qb tab and background annotation loop skip:

- Files ending in `.!qB` (qBittorrent's incomplete-file marker)
- Any path with an `incomplete/` component
- Dotfiles
- Files whose mtime is within the last 60 seconds (still being written)
(yt staging files live in `/app/data/downloads`, not `/qb`, so they're not scanned in the first place.)

Video extensions recognized: `.mp4 .mkv .avi .mov .ts .webm`.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is hardcoded to `medium`. Larger = better accuracy + much longer GPU time.
- qb mode ignores embedded subtitle tracks (muxed into the video container) and non-standard sub layouts like `Subs/<episode>/2_English.srt` — only same-stem sidecar `.srt` files are recognized.
