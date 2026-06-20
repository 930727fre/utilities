# transcribe

Three ways in:

- **yt tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **bt tab** — paste a magnet link. We spawn a one-shot `aria2c` subprocess that writes into `data/bt/<per-torrent-wrapper>/` and keeps seeding until its limit (1440 min or ratio 1.0). The bt tab lists every wrapper folder; phase comes from filesystem inspection — `.aria2` control file present means downloading, gone means seeding, subprocess exit means done.
- **bt tab → "translate to 中" button per torrent** — for shows whose English+annotation pass isn't enough (Sopranos-grade dialect / mob slang), click the per-torrent button to produce `<stem>.zh-tw.srt` sidecars in place. Gemini Flash Lite translates the carried-over English `<stem>.srt` cue-by-cue; ~$0.01 and ~1 min per video. No separate folder, no whisper fallback.

For the yt + bt branches, Claude annotation runs automatically once a transcript exists. The background loop also catches externally-arriving SRTs (torrent-bundled subs, manual drops). Cost per ~1-hour SRT is around $0.05.

`data/downloads/` holds the yt-tab output; `data/bt/` holds the BT downloads (mounted at `/bt`), with `.zh-tw.srt` sidecars sitting next to their videos when produced. [webdav](../webdav) reads both for Infuse playback (read-only). The bt-side pipeline never writes to `jobs.json` — every torrent is a directory under `/bt` plus (while live) an in-memory `subprocess.Popen` handle. Chinese sub state is also a filesystem-only signal: `<stem>.zh-tw.srt` present = done, `<stem>.zh-tw.srt.error` present = failed.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React — two tabs: yt (URL submit) and bt (magnet submit + library browser; per-torrent "translate to 中" button on annotated torrents) |
| Backend | FastAPI on port 8000 — API + in-process orchestrator |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes our per-job state mutations |
| Downloader | `yt-dlp` (yt) + one-shot `aria2c` subprocess per magnet (bt, 1440 min / ratio 1.0 seed limits) |
| Transcriber | HTTP POST to the shared [whisper](../whisper) service (`faster-whisper-large-v3-turbo`) |
| Annotator | Claude (sonnet) via tool-use, chunked by cue count |
| SRT matcher | Claude (haiku) tool-use agent — `list_dir` + `read_lines` to browse the torrent's tree (flat sibling, RARBG's `Subs/<stem>/N_English.srt`, anything in between), reading cue text to verify language. Sandboxed to the video's folder, capped at 12 tool calls |
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
  Show.S01E01.srt                ← written by bt pipeline (whisper / OS /
                                    bundled-agent), annotated in place
  Show.S01E01.zh-tw.srt          ← written by the "translate to 中" button
                                    (OS Chinese first, Gemini fallback)

  (for multi-file torrents, aria2 adds another nested folder named after
   the torrent's metadata "name" field inside <wrapper>; harmless, predictable)
```

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Infuse auto-loads any `.srt` sibling as a sidecar.

## Run

Prereqs:
- External Docker network `my_network`.
- The shared [whisper](../whisper) service must be running first — startup health-checks it and crashes if unreachable.
- `ANTHROPIC_API_KEY` exported in the shell — required for annotation (Sonnet) + srt-matcher agent + OS subs verifier (both Haiku).
- `GEMINI_API_KEY` exported — required for the bt "translate to 中" button's Gemini fallback path.
- `OPENSUBTITLES_API_KEY` / `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD` exported — required for the OpenSubtitles step. Get an API key by registering a Consumer at https://www.opensubtitles.com/.

All five use compose's `${VAR:?err}` syntax → missing any of them fails the `docker compose up` at parse time with a clear message.

```sh
export ANTHROPIC_API_KEY=…
export GEMINI_API_KEY=…
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
| `POST` | `/api/bt/translate-zh` | `{wrapper}`: queue every video in this torrent for Chinese translation (OS first, Gemini fallback). Refuses if torrent still downloading or any video lacks `※ annotated`. Idempotent — clicking again clears `.error` stamps + the subs_finder cache so it doubles as retry |

## Job states

```
yt:        PENDING → DOWNLOADING → TRANSCRIBING → ANNOTATING → SUCCESS
                                                 ↘ FAILED
bt manual: PENDING → TRANSCRIBING → ANNOTATING → SUCCESS     (legacy /api/bt/transcribe path)
                                   ↘ FAILED
```

Magnet submissions don't enter `jobs.json` at all. The bt-tab UI shows two live views: the aria2c subprocess's torrent list (downloading + seeding state, from `/api/bt/torrents`) and the filesystem scan (annotation + Chinese-sub state, from `/api/bt`). Whisper + annotation per video are queued by the background `_bt_work_loop` once files land in `/bt`. Chinese translation is button-triggered, not scan-triggered — every click submits the wrapper's videos to the translator executor.

Crashed `PENDING` / `DOWNLOADING` / `TRANSCRIBING` jobs flip to `FAILED` on startup. `ANNOTATING` crashes flip to `SUCCESS` with `annotation_error` set; for bt jobs the background loop will retry.

## Annotation

Claude scans the SRT for U.S.-cultural references a Taiwanese viewer might miss — athletes, brands, regional places, slang, sports gameplay — and appends a short 繁體中文 note prefixed with `※` to the relevant cues. After annotation, sentinel cues sit in the 00:00:00–00:00:08 window at video start: `※ source: <bundled | opensubtitles-hash | opensubtitles-text | whisper>` (0–2 s) recording which pipeline path produced the SRT — `bundled` for the srt-matcher tool-use agent picking from the torrent's own files, `opensubtitles-*` for OS, `whisper` for the GPU fallback. `※ annotated` (2–4 s) marking the annotation pass complete. On the bt path when whisper took over because OpenSubtitles missed, `※ os failed: <reason>` (4–8 s) recording exactly why OS didn't deliver (no candidate / verifier rejection / quota / etc.) — OS is the most consequential leg of the pipeline (human subs > whisper) so its failure mode is surfaced first-class. They flash at playback start so the user gets immediate confirmation of "which path produced this SRT, did annotation run, did OS try" without opening the UI. Manually-dropped SRTs carry no source tag. The presence of `※ annotated` on disk is the only annotation-state signal — no jobs.json overlay. Failures use the same window (whisper failure as the sole cue 0–3 s; annotate failure appended at 2–5 s after the source stamp).

A background loop scans `/bt` every 30s for both whisper work (video without SRT) and annotation work (SRT without `※ annotated`), and queues each through the appropriate executor. Failures are recorded as sentinel cues inside the SRT itself (`※ whisper failed:` / `※ annotate failed:`) so the loop knows not to retry; the UI ↻ button is the only path back into the pipeline.

**Before queuing whisper, two rescue steps fire** (in order, cheapest first):

1. **LLM srt-matcher**: Claude Haiku gets `list_dir` + `read_lines` tools and walks the video's folder tree to find an English subtitle. Handles flat-folder bundles (`Movie.2024.en.srt` next to the mp4), RARBG-style nested layouts (`Subs/<video-stem>/N_English.srt`), and anything in between the same way — the agent lists what's around, drills into likely subfolders, and reads the first ~20 cues of candidates to verify the language is English (skip Spanish / Chinese / SDH-only / forced-signs tracks). Match → COPY (not move) to satisfy strict match while preserving the torrent's original Subs/ folder → annotation proceeds. Tool calls are sandboxed to the video's parent folder and capped at 12.
2. **OpenSubtitles (hash → text)**: if no sibling `.srt` to rescue, compute the file's OSDb hash and query OpenSubtitles. Filter to `moviehash_match=true` results (uploader-claimed exact hash match), then ask Claude Haiku to confirm the candidate's release / title / year / show / S+E matches the local filename — `moviehash_match` is uploader-claimed, not server-verified, and mis-tagged uploads do exist (real case: Spider-Man subs returned for a Whiplash hash). If hash search returns nothing or Haiku rejects every candidate, fall back to a **text search** by extracted title (+ year for movies, + season/episode for TV), again gated by the verifier. Source-stamped `※ source: opensubtitles-hash` vs `opensubtitles-text` so you can tell which path produced the SRT. On confirmed match: download → run `ffsubsync` against the video's audio to correct any release-mismatch timing drift → annotation. Misses are cached in-memory so we don't burn quota on the same file every 30s.

Only if both steps miss does whisper run. For popular content (movies, mainstream TV) this means ~zero GPU is spent — OpenSubtitles' human-translated subs are higher quality than whisper output anyway.

## bt scan filters

The bt tab and background annotation loop skip:

- Dotfiles
- Any video whose wrapper folder still contains a `.aria2` control file anywhere (aria2c uses ONE control file per torrent, not per-file; its presence anywhere under the wrapper means the whole torrent is still mid-download / mid-verify)
- Files whose mtime is within the last 60 seconds (belt-and-suspenders for non-aria2c writers — manual drops, webdav copies, rsync)
(yt staging files live in `/app/data/downloads`, not `/bt`, so they're not scanned in the first place.)

Video extensions recognized: `.mp4 .mkv .avi .mov .ts .webm`.

## Chinese translation ("translate to 中" button)

For shows whose English+annotation pass isn't enough (Sopranos, The Wire, anything with thick dialect / mob slang / AAVE), each bt torrent card carries a per-torrent "→ 中" button (visible when every video in the wrapper has `※ annotated`). One click queues every video for Chinese translation; results land as `<stem>.zh-tw.srt` sidecars next to the videos. Infuse picks both `<stem>.srt` (English + ✨) and `<stem>.zh-tw.srt` (Chinese) as separate language tracks on the same video.

**Gemini Flash Lite only — no OpenSubtitles lookup.** The OS step was tried and dropped: Chinese-sub uploads on OpenSubtitles are sparse and frequently mistagged for the releases the user actually watches, so the result was either no candidate or a wrong-content match the verifier had to reject anyway. Going straight to Gemini gives a more predictable result. Cue timing is inherited verbatim from the English SRT (which was already release-aligned by the bt pipeline) — no ffsubsync needed.

Per video:
1. Look up sibling `<stem>.srt` (the English transcript bt produced).
2. Chunk it 400 cues at a time; send each chunk to Gemini Flash Lite for forced-JSON translation; preserve cue line structure + pass `※` sentinel cues through unchanged.
3. Write `<stem>.zh-tw.srt` next to the video and stamp `※ source: llm-translated` so the player flashes the source tag at the 0–2 s window same as everywhere else.

Cost: ~$0.01 per movie, ~1 minute per movie. A 13-episode pack like Sopranos S01 takes ~13 min end-to-end.

`<stem>.zh-tw.srt.error` sentinel files (plain text, holding the failure reason) appear on either-step failures. The button doubles as retry: clicking again clears `.error` stamps before re-queueing, so a failed video gets a fresh attempt.

**Per-torrent button + per-video `zh_in_flight` state**: the backend keeps an in-memory map of `path → Future` for every video currently mid-translation. The bt scan exposes `zh_in_flight: bool` per video so the UI can show a pulsing `中` while Gemini is working AND disable the per-torrent button (with a "Translation in progress…" tooltip) while any of its videos are still in flight. The `finally` clause inside the submission wrapper pops the entry when the worker exits, so the flag is self-cleaning — no useEffect diffing on the client.

Single worker (`translator_executor` with `max_workers=1`) serializes translations to avoid Gemini rate limits and keep the UI's `中` flips predictable.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is whatever the shared [whisper](../whisper) service is configured with (`large-v3-turbo` at time of writing). Change it there, not here.
- bt mode ignores embedded subtitle tracks (muxed into the video container). Non-standard external layouts like `Subs/<episode>/N_English.srt` ARE handled — the srt-matcher tool-use agent finds and copies them into place — but anything muxed into the mkv container still needs `ffmpeg` extraction outside this pipeline.
- The Chinese translation cascade has no whisper fallback (whisper produces English, which would defeat the point) — the Gemini fallback covers it. If Gemini also fails (rare), the video lands at `中 !` and stays there until the user clicks the torrent's button again to retry.
