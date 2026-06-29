# transcribe

Three ways in:

- **yt tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **bt tab** — paste a magnet link. We spawn a one-shot `aria2c` subprocess that writes into `data/bt/<per-torrent-wrapper>/` and keeps seeding until its limit (1440 min or ratio 1.0). The bt tab lists every wrapper folder; phase comes from filesystem inspection — `.aria2` control file present means downloading, gone means seeding, subprocess exit means done.
- **bt tab → "translate to 中" button per torrent** — for shows whose English+annotation pass isn't enough (Sopranos-grade dialect / mob slang), click the per-torrent button to produce `<stem>.zh-tw.srt` sidecars in place. Gemini Flash Lite (10-cue batches with sliding-window context + count/index validation) translates the carried-over English `<stem>.srt`; ~$0.02 and ~30-60 s per video. No separate folder, no whisper fallback.

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
| SRT matcher | Claude (haiku) at bt_filter time — one call picks the canonical title / year / S+E for each main video AND chooses which bundled SRT (if any) to copy into `_sources/<stem>.bundled.srt` as a candidate. Uses cue count + time-span coverage + first-cue preview to distinguish forced / SDH / full-dialogue tracks |
| Subs finder | OpenSubtitles REST API — fetches hash-search and text-search candidates into `_sources/<stem>.opensubtitles-{hash,text}.srt`. Never writes the canonical SRT directly |
| Metadata verifier | Claude (haiku) — confirms an OS search-result's release / title / year matches the local filename. Runs BEFORE download to avoid burning OS quota on the wrong file |
| Content verifier | `jiwer` — cue-density gate plus word error rate (WER) between each candidate's full transcript and the whisper ground-truth transcript. Same library the whisper / Common Voice / ASR-eval ecosystem uses; calibrated threshold from the literature. The whisper SRT IS the trust gate; candidates only become the canonical SRT after passing |
| Subs sync | `ffsubsync` — VAD on the video's audio aligns the verified candidate's cue timing before it lands at the canonical path (handles release-mismatch drift) |
| Storage | `data/jobs.json` (file-locked) + on-disk video + sidecar SRT |

## On-disk layout

```
data/downloads/                                 ← YouTube output
  <sanitized title>.mp4
  <sanitized title>.srt                         ← whisper + annotated in place

data/bt/<wrapper>/                              ← aria2 download dir (READ-ONLY
  Show.S01E01.mkv                                 to us; we hardlink out)
  Subs/                                           the wrapper stays so aria2
                                                  can keep seeding

data/artifact/Movies/Title (Year)/              ← Jellyfin scans here
  Title (Year).mkv                              hardlinked from data/bt
  Title (Year).srt                              FINAL (verified + annotated)
  Title (Year).zh-tw.srt                        from "translate to 中" button

data/artifact/TV/Title (Year)/Season 01/
  Title (Year) - S01E01.mkv
  Title (Year) - S01E01.srt
  Title (Year) - S01E01.annotate-failed         sidecar IF annotation crashed
  Title (Year) - S01E01.whisper-failed          sidecar IF whisper crashed
                                                (extension-less so Jellyfin
                                                ignores them)

data/artifact/_processed/                       pipeline state
  <wrapper>.filtered                            bt_filter sentinel (one per
                                                bt wrapper that's been
                                                LLM-classified)

data/artifact/_sources/                         per-stage pipeline output —
  Movies/Title (Year)/                          mirrors Movies/TV.
    Title (Year).whisper.srt                    raw (whisper output)
    Title (Year).bundled.srt                    raw (from /bt)
    Title (Year).opensubtitles-hash.srt         raw (OS hash hit)
    Title (Year).opensubtitles-text.srt         raw (OS text hit)
    Title (Year).verified.srt                   processed (winner picked +
                                                  ffsubsync'd; annotation
                                                  reads this and writes
                                                  canonical above)
  TV/Title (Year)/Season 01/                    each file is the cached
    Title (Year) - S01E01.whisper.srt           output of one pipeline
    Title (Year) - S01E01.bundled.srt           stage. Delete any one to
    Title (Year) - S01E01.verified.srt          replay that stage forward;
                                                earlier-stage cache is kept
```

Jellyfin's docker-compose mounts only `data/artifact/Movies` and `data/artifact/TV` — `_processed` and `_sources` are invisible to it.

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Infuse auto-loads any `.srt` sibling as a sidecar.

## Run

Prereqs:
- External Docker network `my_network`.
- The shared [whisper](../whisper) service must be running first — startup health-checks it and crashes if unreachable.
- `ANTHROPIC_API_KEY` exported in the shell — required for annotation (Sonnet), srt-matcher agent (Haiku), OS subs verifier (Haiku).
- `GEMINI_API_KEY` exported — required for the bt "translate to 中" button (10-cue Gemini Flash Lite batches). Get one at https://aistudio.google.com/apikey.
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
| `POST` | `/api/bt/retry` | `{path}`: clear the canonical SRT + both failure sidecars (whisper-failed / annotate-failed) so the loop replays the pipeline. Cached `_sources/` candidates are preserved for cheap replay |
| `POST` | `/api/bt/upgrade-english` | `{wrapper}`: nuke cached OS candidates + canonical SRTs in the wrapper so the next tick re-fetches OS against today's quota. Throws away annotation work in the process |
| `POST` | `/api/bt/translate-zh` | `{wrapper}`: queue every video in this torrent for Chinese translation via 10-cue Gemini Flash Lite batches. Refuses if torrent still downloading or any video lacks `※ annotated`. Idempotent — clicking again clears `.error` stamps so it doubles as retry |

## Job states

```
yt:        PENDING → DOWNLOADING → TRANSCRIBING → ANNOTATING → SUCCESS
                                                 ↘ FAILED
bt manual: PENDING → TRANSCRIBING → ANNOTATING → SUCCESS     (legacy /api/bt/transcribe path)
                                   ↘ FAILED
```

Magnet submissions don't enter `jobs.json` at all. The bt-tab UI shows two live views: the aria2c subprocess's torrent list (downloading + seeding state, from `/api/bt/torrents`) and the filesystem scan (annotation + Chinese-sub state, from `/api/bt`). Whisper + annotation per video are queued by the background `_bt_work_loop` once files land in `/bt`. Chinese translation is button-triggered, not scan-triggered — every click submits the wrapper's videos to the translator executor.

Crashed `PENDING` / `DOWNLOADING` / `TRANSCRIBING` / `ANNOTATING` jobs flip to `FAILED` on startup. For bt jobs the background scan tick re-queues the pipeline, which resumes from whichever stage's output is missing under `_sources/`.

## SRT pipeline (bt path)

Whisper is the ground-truth listening reference; scraped subtitles are "literary upgrades" we accept only after they prove they're subtitling the same audio. Each stage's output is cached under `/artifact/_sources/`, so any partial progress survives crashes / restarts — the pipeline picks up at the first stage whose output is missing.

```
1. bt_filter (Haiku, one call per wrapper, at torrent-finish time)
     → hardlinks main-feature videos into Movies/ or TV/
     → copies bundled English SRT (if any) into _sources/<stem>.bundled.srt

2. whisper (HTTP to shared service, GPU-gated)
     → _sources/<stem>.whisper.srt

3. OS candidate fetch (lazy, per video)
     → _sources/<stem>.opensubtitles-hash.srt   (if hash search hits)
     → _sources/<stem>.opensubtitles-text.srt   (if text search hits)
   Each search is metadata-prefiltered by Haiku (`subs_verifier.verify_candidate`)
   to avoid burning OS quota on the wrong file.

4. Content gate (jiwer / WER, deterministic, ~$0)
     For each candidate in order (bundled → OS hash → OS text):
       - cue-density gate: reject if candidate has <40% or >250% of
         whisper's cue count (catches forced subs / wrong content)
       - concat all cue text → strip SRT formatting + punctuation,
         lowercase → compute WER vs whisper (reference) → pass if
         WER ≤ 0.5
     Timing is ignored on purpose — ffsubsync handles alignment later;
     verify's only job is "same transcript content."
     First-pass-wins. First passing candidate → ffsubsync against the
     video's audio → _sources/<stem>.verified.srt.
     All candidates fail → cp whisper.srt → verified.srt.

5. Annotation (Sonnet) — reads _sources/<stem>.verified.srt, returns
   annotated SRT text. NO marker cue inserted.
     → atomic write to /artifact/.../<stem>.srt (canonical)
```

Canonical SRT existence IS the "fully done" signal — no marker reads anywhere. Atomic write (tmp file + rename) means any downstream reader (Jellyfin scan, Infuse browse) sees either the prior file or the new fully-annotated file, never a half-written intermediate.

## State model

The background loop inspects the filesystem alone — no jobs.json overlay, no SRT-body parsing:

```
canonical /artifact/.../<stem>.srt exists       → done; skip
<stem>.whisper-failed sidecar exists            → pipeline halted at whisper
                                                   (skip until ↻)
<stem>.annotate-failed sidecar exists           → pipeline halted at annotation
                                                   (skip until ↻; _sources/.verified.srt
                                                   is kept so ↻ only re-does annotation)
(none of the above)                              → queue process_bt_file; it
                                                   resumes at the first missing
                                                   _sources/ stage
```

Failure sidecars are extension-less plain-text files holding the error reason — Jellyfin / Infuse never load them as subtitles, but `cat <stem>.whisper-failed` shows you what broke. The UI ↻ button clears canonical + both sidecars; cached `_sources/` files are preserved so replay only re-does the missing stages.

Manual SRT drops at the canonical path are trusted as final — pipeline doesn't touch them. Drop into `_sources/<stem>.bundled.srt` instead if you want the WER content gate to evaluate your candidate (and to get the automatic annotation pass).

## Rollback granularity

Each pipeline stage has its own cached output under `_sources/`. Delete just what you want to re-run:

| Re-run | Delete |
|---|---|
| Just re-annotate | canonical `/artifact/.../X.srt` (≈ $0.05 Sonnet) |
| Re-verify + re-ffsubsync + re-annotate | `_sources/X.verified.srt` + canonical (≈ $0.05 + ffsubsync) |
| Refetch OS candidates from scratch | `_sources/X.opensubtitles-*.srt` + `_sources/X.verified.srt` + canonical (OS quota + downstream) |
| Re-whisper everything | `rmtree _sources/<path>` + canonical (GPU + full pipeline) |

The `/api/bt/upgrade-english` endpoint automates the "OS refetch" row for an entire torrent wrapper.

## Annotation

Claude (sonnet) reads the verified SRT and appends short `※`-prefixed 繁體中文 notes inline to cues that reference U.S.-cultural specifics a Taiwanese viewer would miss — athletes, brands, regional places, slang, sports gameplay. Empty-array output is fine (and common — most cues need no annotation). The annotation pass runs inline within `process_bt_file` (no separate executor), so the canonical SRT only ever exists in its fully-annotated form.

## bt scan filters

The bt tab and background annotation loop skip:

- Dotfiles
- Any video whose wrapper folder still contains a `.aria2` control file anywhere (aria2c uses ONE control file per torrent, not per-file; its presence anywhere under the wrapper means the whole torrent is still mid-download / mid-verify)
- Files whose mtime is within the last 60 seconds (belt-and-suspenders for non-aria2c writers — manual drops, webdav copies, rsync)
(yt staging files live in `/app/data/downloads`, not `/bt`, so they're not scanned in the first place.)

Video extensions recognized: `.mp4 .mkv .avi .mov .ts .webm`.

## Chinese translation ("translate to 中" button)

For shows whose English+annotation pass isn't enough (Sopranos, The Wire, anything with thick dialect / mob slang / AAVE), each bt torrent card carries a per-torrent "→ 中" button (visible when every video in the wrapper has a canonical SRT — which under the new pipeline implies the English transcript is verified + annotated). One click queues every video for Chinese translation; results land as `<stem>.zh-tw.srt` sidecars next to the videos. Infuse picks both `<stem>.srt` (English + ✨) and `<stem>.zh-tw.srt` (Chinese) as separate language tracks on the same video.

**Gemini Flash Lite, 10-cue batches with sliding-window context — no OpenSubtitles lookup.** The OS step was tried and dropped: Chinese-sub uploads on OpenSubtitles are sparse and frequently mistagged for the releases the user actually watches. Cue timing is inherited verbatim from the English SRT (which was already release-aligned by the bt pipeline) — no ffsubsync needed.

**Why 10-cue batches.** We went through four architectures arriving here:

1. 400→200 cues per Flash Lite call: silently dropped short cues from JSON output then renumbered the rest, drifting subtitle content off the spoken dialogue.
2. 200 cues per Haiku call: kept the entry count right but occasionally shifted content inside a chunk.
3. 1 cue per Gemini call (industry-standard per-segment): structurally bulletproof alignment, but ~17:1 input/output token overhead — sending ~530 prompt tokens to translate one ~30-token cue. 818 calls per episode burns through API quota and incurs more 503 retries than the smaller call count.
4. **10 cues per call (this).** Middle ground: ~10× fewer API calls than per-cue, ~3× cheaper token-wise, blast radius of any per-batch alignment failure capped at 10 cues, and a count+index validator + one retry catches the rare cases where Gemini drops a short cue.

Each batch call carries 3 cues before + 3 cues after the target batch as REFERENCE (not to translate, only as context) — the batch itself provides 10 cues of internal context, so the additional window can be smaller than per-cue's 5+5.

Per video:
1. Look up sibling `<stem>.srt` (the English transcript bt produced).
2. Identify cues to translate (skip `※`-prefixed sentinels — they pass through verbatim).
3. Chunk into 10-cue batches; run batches in parallel (10 concurrent Gemini calls per episode), each with sliding-window context + forced-JSON array output keyed by the input cue indices.
4. Validate per batch: returned cue indices must cover the input batch. On mismatch, retry once before accepting partial coverage (missing cues keep their English line).
5. Apply translations back in place.
6. Write `<stem>.zh-tw.srt` next to the video.

Cost: ~$0.02 per movie, ~30-60 s per movie depending on cue count. A 13-episode pack like Sopranos S01 takes ~10 min end-to-end.

`<stem>.zh-tw.srt.error` sentinel files (plain text, holding the failure reason) appear on either-step failures. The button doubles as retry: clicking again clears `.error` stamps before re-queueing, so a failed video gets a fresh attempt.

**Per-torrent button + per-video `zh_in_flight` state**: the backend keeps an in-memory map of `path → Future` for every video currently mid-translation. The bt scan exposes `zh_in_flight: bool` per video so the UI can show a pulsing `中` while translation is in progress AND disable the per-torrent button (with a "Translation in progress…" tooltip) while any of its videos are still in flight. The `finally` clause inside the submission wrapper pops the entry when the worker exits, so the flag is self-cleaning — no useEffect diffing on the client.

Two workers at the episode level (`translator_executor` with `max_workers=2`); inside each, 10 concurrent batch-level calls (`_BATCH_CONCURRENCY` in translator.py). At peak that's 20 Gemini Flash Lite calls in flight — well under any quota and within the sustained-throughput window where Google doesn't throttle.

## Known limitations

- `jobs.json` is file-locked, not a real DB. Single-user is fine; for concurrent users move to SQLite.
- Whisper model is whatever the shared [whisper](../whisper) service is configured with (`large-v3-turbo` at time of writing). Change it there, not here.
- bt mode ignores embedded subtitle tracks (muxed into the video container). Non-standard external layouts like `Subs/<episode>/N_English.srt` ARE handled — the srt-matcher tool-use agent finds and copies them into place — but anything muxed into the mkv container still needs `ffmpeg` extraction outside this pipeline.
- The Chinese translation cascade has no whisper fallback (whisper produces English, which would defeat the point). If a batch's API call fails or the validator can't recover from a missing-cue response, the missing cues silently keep their English lines (the rest of the SRT is still useful). If the whole translation raises (e.g. all calls hit a long Gemini outage), the video lands at `中 !` and stays there until the user clicks the torrent's button again to retry.
