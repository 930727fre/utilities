# transcribe

Two ways in:

- **yt tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **qb tab** — view-only: every video in qBittorrent's downloads (`/qb`). No buttons; a background loop handles everything automatically — videos without SRT get Whisper'd, SRTs without `※ annotated` get annotated.

In both cases, Claude annotation runs automatically once a transcript exists. The background loop also catches externally-arriving SRTs (torrent-bundled subs, manual drops). Cost per ~1-hour SRT is around $0.05.

`data/downloads/` holds the yt-tab output; `/qb` is the same folder qBittorrent writes to. Both are reached by [webdav](../webdav) for Infuse playback (read-only mount of `/qb`).

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
| Subs finder | OpenSubtitles REST API — queries by OSDb file hash for human-translated subs before falling back to whisper |
| Subs verifier | Claude (haiku) — confirms the OpenSubtitles candidate's release/title/year matches the local filename, guarding against mis-tagged uploads |
| Subs sync | `ffsubsync` — VAD on the video's audio aligns the downloaded SRT's cue timing (handles release-mismatch drift) |
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

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Infuse auto-loads any `.srt` sibling as a sidecar.

## Run

Prereqs:
- External Docker network `my_network`.
- The shared [whisper](../whisper) service must be running first — startup health-checks it and crashes if unreachable.
- Sibling `qbittorrent/` directory exists (the compose bind-mounts `../qbittorrent/data/downloads`).
- qBittorrent's "Append .!qB extension to incomplete files" enabled (so the qb scan skips in-flight downloads).
- `ANTHROPIC_API_KEY` exported in the shell — required for annotation.
- `OPENSUBTITLES_API_KEY` / `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD` exported — required for the OpenSubtitles step. Get an API key by registering a Consumer at https://www.opensubtitles.com/.

All four use compose's `${VAR:?err}` syntax → missing any of them fails the `docker compose up` at parse time with a clear message.

```sh
export ANTHROPIC_API_KEY=…
export OPENSUBTITLES_API_KEY=…
export OPENSUBTITLES_USERNAME=…
export OPENSUBTITLES_PASSWORD=…
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

Claude scans the SRT for U.S.-cultural references a Taiwanese viewer might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues. After annotation, two far-future sentinel cues (at 99:59:58 / 99:59:59, never displayed during playback) sit at the end of every processed SRT: `※ source: <whisper|opensubtitles>` recording which pipeline step produced the SRT (only if we produced it; bundled / manually-dropped SRTs carry no source tag), and `※ annotated` marking the annotation pass complete. The presence of `※ annotated` on disk is the only annotation-state signal — no jobs.json overlay.

A background loop scans `/qb` every 30s for both whisper work (video without SRT) and annotation work (SRT without `※ annotated`), and queues each through the appropriate executor. Per-path failure counters (whisper / annotation tracked separately) cap at 3 attempts each (in-memory; container restart resets them).

**Before queuing whisper, two rescue steps fire** (in order, cheapest first):

1. **LLM srt-matcher**: if there's at least one `.srt` in the same folder that strict same-stem matching missed (e.g. release-bundled `Movie.2024.en.srt`), Claude Haiku checks whether any sibling `.srt` is actually this video's subtitle. Match → rename to satisfy strict match → annotation proceeds.
2. **OpenSubtitles (hash → text)**: if no sibling `.srt` to rescue, compute the file's OSDb hash and query OpenSubtitles. Filter to `moviehash_match=true` results (uploader-claimed exact hash match), then ask Claude Haiku to confirm the candidate's release / title / year / show / S+E matches the local filename — `moviehash_match` is uploader-claimed, not server-verified, and mis-tagged uploads do exist (real case: Spider-Man subs returned for a Whiplash hash). If hash search returns nothing or Haiku rejects every candidate, fall back to a **text search** by extracted title (+ year for movies, + season/episode for TV), again gated by the verifier. Source-stamped `※ source: opensubtitles-hash` vs `opensubtitles-text` so you can tell which path produced the SRT. On confirmed match: download → run `ffsubsync` against the video's audio to correct any release-mismatch timing drift → annotation. Misses are cached in-memory so we don't burn quota on the same file every 30s.

Only if both steps miss does whisper run. For popular content (movies, mainstream TV) this means ~zero GPU is spent — OpenSubtitles' human-translated subs are higher quality than whisper output anyway.

## qb scan filters

The qb tab and background annotation loop skip:

- Files ending in `.!qB` (qBittorrent's incomplete-file marker)
- Any path with an `incomplete/` component
- Dotfiles
- Files whose mtime is within the last 60 seconds (still settling — covers post-rename windows, in-progress large copies)
(yt staging files live in `/app/data/downloads`, not `/qb`, so they're not scanned in the first place.)

Video extensions recognized: `.mp4 .mkv .avi .mov .ts .webm`.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is whatever the shared [whisper](../whisper) service is configured with (`large-v3-turbo` at time of writing). Change it there, not here.
- qb mode ignores embedded subtitle tracks (muxed into the video container) and non-standard sub layouts like `Subs/<episode>/2_English.srt` — only same-stem sidecar `.srt` files are recognized.
