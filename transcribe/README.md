# transcribe

Three ways in:

- **yt tab** — paste a URL, get an mp4 + Whisper SRT in `data/downloads/`, named after the video.
- **bt tab** — paste a magnet link. Handed off to the sibling [aria2](../aria2) service (BT traffic exits through Surfshark VPN over there); it spawns a one-shot `aria2c` subprocess that writes into `data/bt/<per-torrent-wrapper>/` (bind-mounted between both containers) and keeps seeding until its limit (1440 min or ratio 1.0). The bt tab lists every wrapper folder; phase comes from filesystem inspection — `.aria2` control file present means downloading, gone means seeding, subprocess exit means done.
- **bt tab → "translate to 中" button per torrent** — for shows whose English+annotation pass isn't enough (Sopranos-grade dialect / mob slang), click the per-torrent button to produce `<stem>.zh-tw.srt` sidecars in place. Gemini Flash Lite (10-cue batches with sliding-window context + count/index validation) translates the carried-over English `<stem>.srt`; ~$0.02 and ~30-60 s per video. No separate folder, no whisper fallback.

For the yt + bt branches, Claude annotation runs automatically once a transcript exists. The background loop also catches externally-arriving SRTs (torrent-bundled subs, manual drops). Cost per ~1-hour SRT is around $0.05.

`data/downloads/` holds the yt-tab output; `data/bt/` holds the BT downloads (mounted at `/bt`), with `.zh-tw.srt` sidecars sitting next to their videos when produced. [jellyfin](../jellyfin) reads both for playback. The bt-side pipeline never writes to `jobs.json` — every torrent is a directory under `/bt` plus (while live) an in-memory `subprocess.Popen` handle in the [aria2](../aria2) sidecar. Chinese sub state is also a filesystem-only signal: `<stem>.zh-tw.srt` present = done, `<stem>.zh-tw.srt.error` present = failed.

## Stack

| Layer | Tech |
|------|------|
| Frontend | Vite + React — two tabs: yt (URL submit) and bt (magnet submit + library browser; per-torrent "translate to 中" button on annotated torrents) |
| Backend | FastAPI on port 8000 — API + in-process orchestrator |
| Worker | `ThreadPoolExecutor(max_workers=1)` — serializes our per-job state mutations |
| Downloader | `yt-dlp` (yt, in-container) + sibling [aria2](../aria2) service for bt (one-shot `aria2c` per magnet, 1440 min / ratio 1.0 seed limits; peer traffic exits via Surfshark gluetun so tracker / peer / DHT IPs never leak transcribe's origin) |
| Transcriber | Client-side `ffmpeg -vn -ac 1 -ar 16000` to a small AAC file, then HTTP POST to the shared [whisper](../whisper) service (`faster-whisper-large-v3-turbo`). Pre-transcoding keeps uploads uniformly ~30-50 MB regardless of source size — avoids sporadic connection drops observed on multi-GB Blu-ray mkv uploads |
| Annotator | Claude (sonnet) via tool-use, chunked by cue count |
| Main-feature classifier | Claude (opus) at bt_filter time — one call per torrent regardless of episode count. For single-show TV packs the LLM returns series title / year + a Python regex; code applies the regex to every filename in the wrapper (Friends full 10 seasons = 240 episodes handled in one LLM call). For movies / mixed collections the LLM enumerates per-video. Subtitle selection is NOT bt_filter's job — the WER gate downstream handles "is this `.srt` the right one for this video" via content match |
| Subs finder | `ffmpeg` + `ffprobe` (container-agnostic stream probe + extraction — mkv SubRip, mp4 mov_text, WebM WebVTT all land as SubRip via `-c:s srt`) + `pgsrip` + `tesseract-ocr` (OCR for PGS bitmap subtitle tracks — covers Blu-ray releases like Chernobyl that only mux PGS) + OpenSubtitles REST API (hash + text searches). Container-extracted sources are preferred — same source as the video, byte-perfect timing |
| Content verifier | `jiwer` — word error rate (WER) between each candidate's full transcript and the whisper ground-truth transcript. Same library the whisper / Common Voice / ASR-eval ecosystem uses; calibrated threshold (≤ 0.5) from the literature. The whisper SRT IS the trust gate; candidates only become the canonical SRT after passing. Pollution-window scrubbing salvages partially-hallucinated whisper (see the polluted-scrub explainer in the pipeline section below) |
| Subs sync | `alass` — Rust binary that does piecewise drift detection on the video's audio + candidate's cues, aligning each segment independently. Picked over ffsubsync because it can handle releases that differ in cold-open / recap structure (not just uniform offset drift). Only runs for candidates whose timing isn't already correct (bundled / OS); the embedded extraction path skips alass entirely |
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
    Title (Year).archive.srt                    raw (previously-canonical SRT
                                                  from data/archive/; Gemini
                                                  picks the folder + SxxExx
                                                  key locates the file; first
                                                  tier tried, skips annotate
                                                  when it wins)
    Title (Year).embedded.srt                   raw (mkvextract of the mkv's
                                                  own SubRip track, if any)
    Title (Year).pgs-ocr.srt                    raw (mkvextract of PGS track
                                                  → pgsrip/tesseract OCR;
                                                  ~95% char accuracy)
    Title (Year).bundled.srt                    raw (from /bt)
    Title (Year).opensubtitles-hash.srt         raw (OS hash hit — copy of the
                                                  k-try winner among the indexed
                                                  Title (Year).opensubtitles-hash-<i>.srt
                                                  files, i=1..OS_MAX_TRIES)
    Title (Year).opensubtitles-text.srt         raw (OS text hit; same k-try
                                                  scheme as -hash above)
    Title (Year).verified.srt                   processed (winner picked +
                                                  alass-aligned, or
                                                  embedded/pgs-ocr promoted
                                                  as-is; annotation reads
                                                  this and writes canonical
                                                  above)
  TV/Title (Year)/Season 01/                    each file is the cached
    Title (Year) - S01E01.whisper.srt           output of one pipeline
    Title (Year) - S01E01.bundled.srt           stage. Delete any one to
    Title (Year) - S01E01.verified.srt          replay that stage forward;
                                                earlier-stage cache is kept

data/archive/Title (Year)/                      ← durable SRT preservation
  Title (Year).srt                              (movies: flat)              (auto-mirrored on every
  Title (Year).zh-tw.srt                                                    canonical write; survives
  Season 01/                                    (TV: seasoned tree)         delete_torrent so a
    Title (Year) - S01E01.srt                                               re-download hits the
    Title (Year) - S01E01.zh-tw.srt                                         archive tier for free)
```

Jellyfin's docker-compose mounts only `data/artifact/Movies` and `data/artifact/TV` — `_processed` and `_sources` are invisible to it.

YouTube filename collisions get `(2)`, `(3)`, … suffixes; titles sanitized for filesystem (control chars and `<>:"/\|?*` replaced with `_`, capped at 180 chars). Infuse auto-loads any `.srt` sibling as a sidecar.

## Run

Prereqs:
- External Docker network `my_network`.
- The shared [whisper](../whisper) service must be running first — startup health-checks it and crashes if unreachable.
- `ANTHROPIC_API_KEY` exported in the shell — required for annotation (Sonnet), bt_filter's main-feature classifier (Opus + web search — one call per torrent, generates a regex for TV packs so token/episode-count is decoupled), and the polluted-whisper plot-check fallback (Opus + web search — rare invocation, but Opus is what recalls specific episode plots reliably; Haiku and Gemini were tested and failed on episode-level discrimination).
- `GEMINI_API_KEY` exported — required for the bt "translate to 中" button (10-cue Gemini Flash Lite batches). Get one at https://aistudio.google.com/apikey.
- `OPENSUBTITLES_API_KEY` / `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD` exported — required for the OpenSubtitles step. Get an API key by registering a Consumer at https://www.opensubtitles.com/.

Optional:
- `OS_MAX_TRIES` (default 1) — with a paid OpenSubtitles subscription that lifts daily quota well above 20, set this to 3–5 to k-try the top-N raw OS results per tier (download one, WER-check, first passer wins; stops early). Free-tier users leave this at 1 — even one bad hit per episode chews through 20 downloads fast. Indexed downloads land at `_sources/<stem>.opensubtitles-<mode>-<i>.srt`; the winner is copied to `_sources/<stem>.opensubtitles-<mode>.srt` for the outer pipeline.
- `BT_PIPELINE_ENABLED` (default 1) — set to 0 to make transcribe a **pure aria2 downloader**: torrents keep downloading and seeding, `.filtered` sentinels are NOT written, nothing is hardlinked into `/artifact`, and whisper / annotation / OS lookup / plot-check are all skipped. For a "just fill up bt/ for a while" period. Manual UI buttons (retry, translate-zh, upgrade-english) deliberately bypass the switch — they express explicit user intent. Set back to 1 and `docker compose up -d` to resume; the scan loop picks up every finished-but-un-filtered wrapper on the next tick.

All five required vars use compose's `${VAR:?err}` syntax → missing any of them fails the `docker compose up` at parse time with a clear message.

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
| `POST` | `/api/bt/magnet` | `{magnet}`: proxied to the aria2 sidecar which spawns an `aria2c` subprocess; returns `{wrapper}` (the per-torrent folder name under `/bt`) |
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
1. bt_filter (Opus + web_search, one call per wrapper, at torrent-finish time)
     LLM sees a compact structural summary of the wrapper (top-level
     folders + video counts + 2 sample filenames per folder) and picks
     a mode:
       - "tv_regex" (single-show TV pack): returns series_title +
         series_year + a Python regex with (?P<season>...) and
         (?P<episode>...) named groups. Code applies the regex to
         every video filename in the wrapper; each match yields a
         canonical TV path. Handles arbitrarily large packs (Friends
         full 10 seasons = 240 episodes in one LLM call).
       - "per_video" (movie / mixed collection): returns a
         main_features[] list with per-video title/year/kind/S+E.
     Both modes also return bonus_dirs (Extras, Featurettes, etc.)
     whose videos are excluded from hardlinking.
     → hardlinks main-feature videos into Movies/ or TV/
     (subtitles NOT touched here — discovered at step 3)

2. whisper (HTTP to shared service, GPU-gated)
     → _sources/<stem>.whisper.srt

3. Embedded text-subtitle extraction (lazy, per video)
     → ffprobe enumerates subtitle streams; first English (or
       undefined) text-codec stream (subrip, mov_text, webvtt, ass) is
       ffmpeg-extracted with `-c:s srt` so any text format lands as
       SubRip at _sources/<stem>.embedded.srt. Container-agnostic
       — mkv, mp4, WebM, mov all handled the same way.

4. PGS-OCR candidate extraction (lazy, per video)
     → If no text subtitle stream exists but a PGS (bitmap) stream
       does, ffmpeg copies the PGS bitstream to a tempfile (raw .sup)
       and feeds it through `pgsrip → tesseract` (English LSTM
       model), writing to _sources/<stem>.pgs-ocr.srt. ~95%
       character accuracy on modern Blu-ray rendering; OCR errors on
       italics + music glyphs.

5. Bundled candidate discovery (lazy, per video)
     → scans the bt-side wrapper for `.srt`, `.ass`, `.ssa` files;
       ASS/SSA get ffmpeg-converted to SRT (override tags stripped)
       before the check; for each, runs the WER gate (step 7) against
       the whisper output; first passing candidate (sorted by filename)
       gets copied to _sources/<stem>.bundled.srt

6. OS candidate fetch (lazy, per video)
     → _sources/<stem>.opensubtitles-hash.srt   (if hash search hits)
     → _sources/<stem>.opensubtitles-text.srt   (if text search hits)
   No LLM metadata prefilter — WER downstream is the trust gate, and
   uploaders have been observed spoofing OS metadata (`release` /
   S+E fields) to game a metadata check anyway. OS tiers run k-try:
   download #1, WER, pass = done; else download #2, etc. Bound by
   `OS_MAX_TRIES`.

7. Content gate (jiwer / WER, deterministic, ~$0)
     For each candidate in order (archive → embedded → pgs-ocr → bundled → OS hash → OS text):
       - reject up-front if the candidate has fewer than 100 real cues
         (forced-subs / partial tracks — would fail WER anyway; saves
         the WER computation)
       - concat all cue text → strip SRT formatting + punctuation,
         lowercase → compute WER vs whisper (reference) → pass if
         WER ≤ 0.5
     Timing is ignored on purpose — alass handles alignment later;
     verify's only job is "same transcript content." WER alone catches
     forced subs (WER ~1 from deletions), bilingual / commentary-bundled
     subs (WER >1 from insertions), and different content (>0.7).
     First-pass-wins. Embedded / pgs-ocr winners are promoted to
     verified.srt verbatim (same-source timing, no alass). Other
     winners go through alass to align against the video's audio
     → _sources/<stem>.verified.srt.
     All candidates fail → cp whisper.srt → verified.srt.

     Pollution-window scrubbing: if whisper's decoder was stuck in a
     hallucination loop (≥10 consecutive identical cues, the classic
     "No.×N" / "Thank you.×N" signature) for some stretch of the
     episode, `find_pollution_windows` returns those time ranges, and
     verify drops cues in those ranges from WHISPER (single-side scrub)
     before computing WER. Candidate stays intact because its timeline
     may not agree with whisper's yet (alass hasn't run — OS / bundled
     tracks can drift a few seconds off the video's master timing).
     Whisper-side scrub inflates WER slightly (candidate's real
     dialogue in the polluted stretch becomes "extra" against a
     shortened reference) but ~5 min pollution in a 50 min episode
     only pushes WER up by ~0.1, well inside the 0.5 pass margin.

     Coverage bail: if pollution windows cover > 50% of the video
     runtime (ffprobe reads `format=duration` once per polluted video
     to decide), no scrub is meaningful — whisper is mostly
     hallucinated and even a perfect candidate scores against a
     shredded reference. WER is disabled and the pipeline falls back
     to a trust + LLM-plot-check loop:
       - archive / embedded / pgs-ocr candidates are TRUSTED (prior
         verified run, or container same-source content guarantee) and
         used directly if they materialize
       - bundled is SKIPPED (multiple SRT candidates in the wrapper,
         no way to pick the right one without WER)
       - opensubtitles-hash / -text run their normal k-try, but the
         accept callback becomes `verify_by_plot` — Opus 4.7 with
         web_search reads the candidate's full dialogue (timestamps
         stripped, ~10-15K tokens for a TV episode) and decides
         whether it matches the target episode's plot. Full dialogue
         beats sampling: sampling could miss the identifying scenes
         on a "quieter" episode, and at Opus's rates (~$5/M input) a
         full episode is a handful of cents per check. Haiku and
         Gemini flash-lite were both empirically inadequate for
         episode-level discrimination (Haiku hallucinated matches on
         wrong episodes of the same show; Gemini said "yes" to any
         recognizable show). Opus recalls specific plot beats
         reliably; web_search fills gaps for episodes outside its
         training coverage. This fallback triggers only on the rare
         > 50% pollution case.

     Below 50% coverage OR no pollution at all → normal WER loop runs;
     any tier can salvage. Whisper with pollution AND no candidate
     passed WER (below the 50% bail) OR the >50% fallback loop didn't
     salvage either → `<stem>.whisper-polluted` sidecar; user drops a
     manual SRT and clicks ↻. See "State model" below.

8. Annotation (Sonnet) — reads _sources/<stem>.verified.srt, returns
   annotated SRT text. NO marker cue inserted.
     → atomic write to /artifact/.../<stem>.srt (canonical)
     SKIPPED when the archive tier won: verified.srt is already an
     annotated SRT from a previous run, so we copy it straight to
     canonical + promote sibling zh-tw if the archive has one. Skips
     the Sonnet annotate + Gemini translate calls (~$0.05-0.10 per
     episode). Archive tier itself does spend one tiny Gemini call for
     title matching (~$0.0005). See `archive.py`.

9. Mirror to data/archive/ (only when a non-archive tier wrote canonical)
     → data/archive/<title>/<season>/<stem>.srt
     Auto-preserves every English + Chinese canonical SRT the pipeline
     ever produces, so a future delete_torrent + re-download hits the
     archive tier at step 7 and short-circuits.
```

Canonical SRT existence IS the "fully done" signal — no marker reads anywhere. Atomic write (tmp file + rename) means any downstream reader (Jellyfin scan, Infuse browse) sees either the prior file or the new fully-annotated file, never a half-written intermediate.

## State model

The background loop inspects the filesystem alone — no jobs.json overlay, no SRT-body parsing:

```
canonical /artifact/.../<stem>.srt exists       → done; skip
<stem>.whisper-failed sidecar exists            → pipeline halted at whisper
                                                   (skip until ↻)
<stem>.whisper-polluted sidecar exists          → whisper hallucinated a loop
                                                   ("No.×N", "Thank you.×N")
                                                   AND either > 50% runtime
                                                   with no plot-check winner,
                                                   or < 50% runtime with no
                                                   scrub-verified candidate.
                                                   Junk whisper was NOT promoted
                                                   to canonical; user must drop
                                                   a candidate or refetch OS,
                                                   then ↻
<stem>.annotate-failed sidecar exists           → pipeline halted at annotation
                                                   (skip until ↻; _sources/.verified.srt
                                                   is kept so ↻ only re-does annotation)
(none of the above)                              → queue process_bt_file; it
                                                   resumes at the first missing
                                                   _sources/ stage
```

Failure sidecars are extension-less plain-text files holding the error reason — Jellyfin / Infuse never load them as subtitles, but `cat <stem>.whisper-failed` (or `.whisper-polluted`, `.annotate-failed`) shows you what broke. The UI ↻ button clears canonical + every sidecar; cached `_sources/` files are preserved so replay only re-does the missing stages.

Manual SRT drops at the canonical path are trusted as final — pipeline doesn't touch them. Drop into `_sources/<stem>.bundled.srt` instead if you want the WER content gate to evaluate your candidate (and to get the automatic annotation pass).

## Rollback granularity

Each pipeline stage has its own cached output under `_sources/`. Delete just what you want to re-run:

| Re-run | Delete |
|---|---|
| Force re-annotate ignoring archive | `data/archive/<title>/…/X.srt` + `_sources/X.archive.srt` + `_sources/X.verified.srt` + canonical |
| Just re-annotate | canonical `/artifact/.../X.srt` (≈ $0.05 Sonnet) |
| Re-verify + re-align + re-annotate | `_sources/X.verified.srt` + canonical (≈ $0.05 + alass) |
| Re-extract embedded SRT from the mkv | `_sources/X.embedded.srt` + `_sources/X.verified.srt` + canonical (free, no quota burn) |
| Re-run PGS OCR on the mkv | `_sources/X.pgs-ocr.srt` + `_sources/X.verified.srt` + canonical (free; CPU-bound, 1-3 min/episode) |
| Refetch OS candidates from scratch | `_sources/X.opensubtitles-*.srt` + `_sources/X.verified.srt` + canonical (OS quota + downstream) |
| Re-whisper everything | `rmtree _sources/<path>` + canonical (GPU + full pipeline) |

The `/api/bt/upgrade-english` endpoint automates the "OS refetch" row for an entire torrent wrapper.

## Annotation

Claude (sonnet) reads the verified SRT and appends short `※`-prefixed 繁體中文 notes inline to cues that reference U.S.-cultural specifics a Taiwanese viewer would miss — athletes, brands, regional places, slang, sports gameplay. Empty-array output is fine (and common — most cues need no annotation). The annotation pass runs inline within `process_bt_file` (no separate executor), so the canonical SRT only ever exists in its fully-annotated form.

## bt scan filters

The bt tab and background annotation loop skip:

- Dotfiles
- Any video whose wrapper folder still contains a `.aria2` control file anywhere (aria2c uses ONE control file per torrent, not per-file; its presence anywhere under the wrapper means the whole torrent is still mid-download / mid-verify)
- Files whose mtime is within the last 60 seconds (belt-and-suspenders for non-aria2c writers — manual drops, rsync)
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
- bt mode handles text-based embedded subtitles across containers (mkv subrip, mp4 mov_text, WebM webvtt, mkv/mp4 ASS/SSA; ffmpeg's `-c:s srt` strips override tags and converts everything to SubRip on extraction) and PGS bitmap tracks via OCR (`pgsrip → tesseract`, second candidate). Heavy ASS typesetting (anime karaoke, sign translations) can leave styling residue — the WER gate catches gross cases and the pipeline falls through to the next candidate. VobSub (DVD-era image format) is skipped — almost no modern rip uses it.
- The Chinese translation cascade has no whisper fallback (whisper produces English, which would defeat the point). If a batch's API call fails or the validator can't recover from a missing-cue response, the missing cues silently keep their English lines (the rest of the SRT is still useful). If the whole translation raises (e.g. all calls hit a long Gemini outage), the video lands at `中 !` and stays there until the user clicks the torrent's button again to retry.
