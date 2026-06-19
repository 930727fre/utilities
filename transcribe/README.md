# transcribe

Three ways in:

- **yt tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **bt tab** — paste a magnet link. We spawn a one-shot `aria2c` subprocess that writes into `data/bt/<per-torrent-wrapper>/` and keeps seeding until its limit (1440 min or ratio 1.0). The bt tab lists every wrapper folder; phase comes from filesystem inspection — `.aria2` control file present means downloading, gone means seeding, subprocess exit means done.
- **translate_zh tab** — manually `mv` a watched-and-annotated folder into `data/translate_zh/`. The scan loop queries OpenSubtitles for human-translated zh-tw/zh-cn subs and saves them as `<stem>.zh-tw.srt` next to each video. No whisper fallback (whisper produces English); no annotation. Use this when the show's English+annotation pass wasn't enough — e.g. The Sopranos.

For the yt + bt branches, Claude annotation runs automatically once a transcript exists. The background loop also catches externally-arriving SRTs (torrent-bundled subs, manual drops). Cost per ~1-hour SRT is around $0.05.

`data/downloads/` holds the yt-tab output; `data/bt/` holds the BT downloads (mounted at `/bt`); `data/translate_zh/` holds the Chinese-fetch branch (mounted at `/translate_zh`). [webdav](../webdav) reads all three for Infuse playback (read-only). The bt-side pipeline never writes to `jobs.json` — every torrent is a directory under `/bt` plus (while live) an in-memory `subprocess.Popen` handle. The translate_zh branch also stays out of `jobs.json`.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React — three tabs: yt (URL submit), bt (magnet submit + library browser), translate_zh (Chinese-subs branch) |
| Backend | FastAPI on port 8000 — API + in-process orchestrator |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes our per-job state mutations |
| Downloader | `yt-dlp` (yt) + one-shot `aria2c` subprocess per magnet (bt, 1440 min / ratio 1.0 seed limits) |
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

data/bt/<wrapper>/               ← per-torrent wrapper; aria2c writes here,
  Show.S01E01.mkv                  transcribe reads via /bt mount
  Show.S01E01.srt                ← written by bt transcribe, or torrent-bundled

  (for multi-file torrents, aria2 adds another nested folder named after
   the torrent's metadata "name" field inside <wrapper>; harmless, predictable)

data/translate_zh/<folder>/      ← user mv's annotated folders in here;
  Show.S01E01.mkv                  scan loop fetches Chinese subs and writes
  Show.S01E01.srt                  the .zh-tw.srt sidecar next to the video.
  Show.S01E01.zh-tw.srt          ← written by translate_zh scan
```

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Infuse auto-loads any `.srt` sibling as a sidecar.

## Run

Prereqs:
- External Docker network `my_network`.
- The shared [whisper](../whisper) service must be running first — startup health-checks it and crashes if unreachable.
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
| `GET`  | `/api/jobs?source=yt\|bt` | List jobs in one bucket; default `yt` |
| `GET`  | `/api/jobs/{id}` | Single job |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed job |
| `DELETE` | `/api/jobs/{id}` | Mark deleted; yt jobs also remove their mp4 + srt |
| `GET`  | `/api/bt` | Scan `/bt`; return file + annotation state |
| `POST` | `/api/bt/magnet` | `{magnet}`: spawn an `aria2c` subprocess; returns `{wrapper}` (the per-torrent folder name under `/bt`) |
| `GET`  | `/api/bt/torrents` | One entry per wrapper folder + its phase (`downloading` / `seeding` / `done` / `orphaned`) |
| `DELETE` | `/api/bt/torrents/{wrapper}` | Kill the subprocess (if running) + rmtree the wrapper folder |
| `POST` | `/api/bt/transcribe` | `{path}`: manually trigger whisper on a bt file (background loop already handles this; useful for power/curl override) |
| `POST` | `/api/bt/retry` | `{path}`: clear the failure sentinel SRT so the loop picks the file up again |
| `GET`  | `/api/translate_zh` | Scan `/translate_zh`; return file + zh-tw.srt sidecar state |
| `POST` | `/api/translate_zh/retry` | `{path}`: clear the cache miss + delete the zh-tw.srt error sentinel so the loop retries |

## Job states

```
yt:        PENDING → DOWNLOADING → TRANSCRIBING → ANNOTATING → SUCCESS
                                                 ↘ FAILED
bt manual: PENDING → TRANSCRIBING → ANNOTATING → SUCCESS     (legacy /api/bt/transcribe path)
                                   ↘ FAILED
```

Magnet submissions don't enter `jobs.json` at all. The bt-tab UI shows two live views: the aria2c subprocess's torrent list (downloading + seeding state, from `/api/bt/torrents`) and the filesystem scan (annotation state, from `/api/bt`). Whisper + annotation per video are queued by the background `_bt_work_loop` once files land in `/bt`. translate_zh files also stay out of `jobs.json` — the scan loop is a tight filesystem-driven check.

Crashed `PENDING` / `DOWNLOADING` / `TRANSCRIBING` jobs flip to `FAILED` on startup. `ANNOTATING` crashes flip to `SUCCESS` with `annotation_error` set; for bt jobs the background loop will retry.

## Annotation

Claude scans the SRT for U.S.-cultural references a Taiwanese viewer might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues. After annotation, sentinel cues sit in the 00:00:00–00:00:08 window at video start: `※ source: <whisper|opensubtitles>` (0–2 s) recording which pipeline step produced the SRT, `※ annotated` (2–4 s) marking the annotation pass complete, and on the bt path when whisper took over because OpenSubtitles missed, `※ os failed: <reason>` (4–8 s) recording exactly why OS didn't deliver (no candidate / verifier rejection / quota / etc.) — OS is the most consequential leg of the pipeline (human subs > whisper) so its failure mode is surfaced first-class. They flash at playback start so the user gets immediate confirmation of "which pipeline produced this SRT, did annotation run, did OS try" without opening the UI. Bundled / manually-dropped SRTs carry no source tag. The presence of `※ annotated` on disk is the only annotation-state signal — no jobs.json overlay. Failures use the same window (whisper failure as the sole cue 0–3 s; annotate failure appended at 2–5 s after the source stamp).

A background loop scans `/bt` every 30s for both whisper work (video without SRT) and annotation work (SRT without `※ annotated`), and queues each through the appropriate executor. Failures are recorded as sentinel cues inside the SRT itself (`※ whisper failed:` / `※ annotate failed:`) so the loop knows not to retry; the UI ↻ button is the only path back into the pipeline.

**Before queuing whisper, two rescue steps fire** (in order, cheapest first):

1. **LLM srt-matcher**: if there's at least one `.srt` in the same folder that strict same-stem matching missed (e.g. release-bundled `Movie.2024.en.srt`), Claude Haiku checks whether any sibling `.srt` is actually this video's subtitle. Match → rename to satisfy strict match → annotation proceeds.
2. **OpenSubtitles (hash → text)**: if no sibling `.srt` to rescue, compute the file's OSDb hash and query OpenSubtitles. Filter to `moviehash_match=true` results (uploader-claimed exact hash match), then ask Claude Haiku to confirm the candidate's release / title / year / show / S+E matches the local filename — `moviehash_match` is uploader-claimed, not server-verified, and mis-tagged uploads do exist (real case: Spider-Man subs returned for a Whiplash hash). If hash search returns nothing or Haiku rejects every candidate, fall back to a **text search** by extracted title (+ year for movies, + season/episode for TV), again gated by the verifier. Source-stamped `※ source: opensubtitles-hash` vs `opensubtitles-text` so you can tell which path produced the SRT. On confirmed match: download → run `ffsubsync` against the video's audio to correct any release-mismatch timing drift → annotation. Misses are cached in-memory so we don't burn quota on the same file every 30s.

Only if both steps miss does whisper run. For popular content (movies, mainstream TV) this means ~zero GPU is spent — OpenSubtitles' human-translated subs are higher quality than whisper output anyway.

## bt scan filters

The bt tab and background annotation loop skip:

- Dotfiles
- Any video that still has a sibling `.aria2` control file (aria2c hasn't finished downloading it)
- Files whose mtime is within the last 60 seconds (belt-and-suspenders for non-aria2c writers — manual drops, webdav copies, rsync)
(yt staging files live in `/app/data/downloads`, not `/bt`, so they're not scanned in the first place.)

Video extensions recognized: `.mp4 .mkv .avi .mov .ts .webm`.

## translate_zh branch

For shows whose English-only annotation pass isn't enough (Sopranos, The Wire, anything with thick dialect / mob slang / AAVE), the translate_zh tab fetches Chinese subs from OpenSubtitles in parallel to the existing English SRT.

Workflow:
1. Watch a folder finish the normal yt or bt annotation pipeline (`<stem>.srt` with `※ annotated` sentinel).
2. `mv` the folder into `data/translate_zh/`.
3. The scan loop (every 30s, same cadence as bt) picks each video up and queries OpenSubtitles with `languages=zh-tw,zh-cn`. The verifier (Claude Haiku) confirms the candidate matches the local release. ffsubsync corrects timing drift. Output saved as `<stem>.zh-tw.srt` next to the video.
4. Infuse picks both `<stem>.srt` (English + ✨) and `<stem>.zh-tw.srt` (Chinese) as separate language tracks.

If no Chinese subs exist on OpenSubtitles (niche shows), the loop writes a `<stem>.zh-tw.srt.error` marker file so the UI can show ! and the loop stops retrying. There's no whisper fallback — whisper produces English, which would defeat the point.

Quota cache: same 24h transient expiry as bt's English path; permanent misses stay permanent until ↻.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is whatever the shared [whisper](../whisper) service is configured with (`large-v3-turbo` at time of writing). Change it there, not here.
- bt mode ignores embedded subtitle tracks (muxed into the video container) and non-standard sub layouts like `Subs/<episode>/2_English.srt` — only same-stem sidecar `.srt` files are recognized.
- translate_zh has no whisper fallback (English would defeat the point) — niche shows with no OpenSubtitles Chinese coverage land at `!` and stay there until the user retries (after Chinese subs become available, or via a manual drop).
