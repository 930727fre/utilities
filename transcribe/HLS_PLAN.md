# hls — Jellyfin replacement for transcribe playback

A thin Python+ffmpeg HLS layer inside transcribe-app that **pre-computes**
each video to HLS once (right after `bt_filter` finishes) and then serves
from cache forever. Replaces Jellyfin's HLS endpoint + UserData KV
without the ~2 GB Jellyfin container.

This file is a spec for a follow-up session. Read it cold — assume zero
context from the prior conversation.

## Goal

transcribe's bt-tab ▸ button currently:

1. POST `/api/play/resolve` (transcribe) → looks up Jellyfin item id by file path
2. GET `/api/play/proxy/{item_id}/master.m3u8` (transcribe → Jellyfin HLS)
3. POST `/api/play/progress` (transcribe → Jellyfin UserData write) + read on resolve

Replace all three with: a static HLS cache directory + a small
`progress.json`.

## Non-goals (explicit)

- No Apple TV / iOS native Jellyfin app compatibility (user uses Infuse on iOS, SMB+IINA on Mac)
- No multi-user / auth (single-user)
- No Jellyfin REST API surface compatibility
- No DLNA / Chromecast / DVR / live TV
- No library scanner / metadata scraping
- No HW acceleration (CPU software encode only)
- No adaptive bitrate (single HLS rendition)
- No on-demand live transcoding logic — everything is pre-computed

## Why pre-compute (the architectural shift)

The prior version of this plan had three implementation phases: v1
(stream as ffmpeg writes), v1.5 (mid-stream scrubber seek via absolute
segment numbering), v2 (HW accel etc.). The scrubber-seek path alone
was ~80% of the engineering complexity.

Pre-computing every video to HLS as part of the post-download pipeline
**eliminates that entire complexity**:

- All segments exist as static files → scrubber works for free
- No `ffmpeg` lifecycle management → no kill / respawn / lock dance
- No absolute segment numbering trick — playlist is static and complete
- No session management — cache files outlive any single playback
- Playback endpoint becomes "FileResponse if it exists, else 404"

The cost is disk + upfront CPU. Both are fine for this setup:
- 148 GB BT library × ~60% compression ratio → ~90 GB cache
- Host disk has 502 GB free
- CPU is idle most of the time; ffmpeg can transcode in background

## Pipeline integration

The post-download workflow already runs as background stages keyed off
filesystem state (`bt_filter` triggered by `.aria2` gone, annotation
triggered by SRT without `※ annotated`, etc.). HLS pre-compute fits in
as another stage:

```
aria2 done (.aria2 gone)
    ↓
bt_filter pass (~10 sec)
  - LLM: srt_matches + bonus_dirs
  - Flatten videos + pipeline siblings to root
  - Whitelist delete
  - Writes .filtered sentinel
    ↓
whisper / OS / annotate              ┐
hls_precompute (NEW, per video)      │  parallel background stages
zh translate (per user click)        ┘
```

HLS pre-compute runs in its OWN executor / loop — does NOT block
`bt_filter`. A 13-episode season's transcode is ~10 hours of CPU; that
should never sit inline in the scan tick.

## Scope: 3 endpoints to deliver

| transcribe needs | new endpoint | replaces |
|------------------|--------------|----------|
| video path → cache identifier | (none — derive from path itself) | `GET /Users` + `GET /Items` |
| read last position | `GET /api/hls/progress?path=...` | `GET /Users/{u}/Items/{i}` |
| write current position | `POST /api/hls/progress` | `POST /UserItems/{i}/UserData` |
| HLS playlist + segments | `GET /api/hls/cache/{wrapper}/{stem}/{filename}` | `GET /Videos/{i}/master.m3u8` + proxy chain |

`/api/play/resolve` keeps its shape but returns
`master_url = /api/hls/cache/{wrapper}/{stem}/master.m3u8`.

## Cache directory layout

```
transcribe/data/hls_cache/
└── <wrapper>/
    └── <video_stem>/
        ├── master.m3u8        ← static when transcode finishes
        ├── seg_0.ts
        ├── seg_1.ts
        └── ...
```

The wrapper / stem naming keeps the cache human-debuggable (you can
`ls` and see which video is which). For the cache id we use the
**transcribe-side relative path**: a video at
`/bt/The.Sopranos.S05.../E01.mkv` maps to
`hls_cache/The.Sopranos.S05.../E01/`. No hash, no opaque id.

## "Is it done?" — no sentinel needed

ffmpeg writes `#EXT-X-ENDLIST` at the end of `master.m3u8` only when
the whole transcode completes. Use the playlist itself as the
completion marker — no `.complete` file to write or race against:

```python
def is_complete(cache_dir: Path) -> bool:
    m = cache_dir / "master.m3u8"
    if not m.exists():
        return False
    return b"#EXT-X-ENDLIST" in m.read_bytes()
```

## Crash recovery: in-memory job registry

`is_complete()` distinguishes "done" from "not done", but not "in
progress" from "crashed mid-write". For that, the precompute loop keeps
an in-memory map:

```python
_hls_jobs: dict[str, subprocess.Popen] = {}  # cache_dir str → ffmpeg proc
```

Scan tick logic for each video in a `.filtered` wrapper:

| cache state | registry state | action |
|-------------|----------------|--------|
| `is_complete()` True | – | skip (done) |
| dir doesn't exist | – | queue new ffmpeg |
| dir exists, no ENDLIST | job in registry, proc alive | skip (in progress) |
| dir exists, no ENDLIST | not in registry / proc dead | rmtree dir + queue new ffmpeg (debris from prior crash) |

Container restart clears `_hls_jobs`; the next scan tick automatically
re-queues anything that didn't get an `#EXT-X-ENDLIST`. Fully idempotent.

## ffmpeg argv

Validated v1 args (Sopranos S05 HEVC 10-bit was the testbed; needs
`-pix_fmt yuv420p` to coerce libx264 to 8-bit output):

```sh
ffmpeg -y \
  -i <source.mkv> \
  -c:v libx264 -pix_fmt yuv420p \
  -b:v 8M -profile:v high -level 4.1 -preset veryfast \
  -c:a aac -b:a 192k -ac 2 \
  -f hls -hls_time 6 -hls_list_size 0 -hls_playlist_type vod \
  -hls_segment_filename '<cache_dir>/seg_%d.ts' \
  '<cache_dir>/master.m3u8'
```

Known edge cases and their fixes (only add when actually hit):
- DTS / TrueHD audio sync drift: `-vsync cfr`
- non-zero timestamp start: `-avoid_negative_ts make_zero`
- libx264 chokes on 10-bit input under profile=high → `-pix_fmt yuv420p` (**always include**)
- HDR source (BT.2020 + PQ/HLG transfer) looks washed-out / grey because
  `-pix_fmt yuv420p` only changes bit depth — color primaries and transfer
  metadata survive into the output. Detect HDR via ffprobe:
  ```python
  out = subprocess.run(
      ["ffprobe", "-v", "error", "-select_streams", "v:0",
       "-show_entries", "stream=color_transfer",
       "-of", "default=nw=1:nk=1", str(video)],
      capture_output=True, text=True,
  ).stdout.strip()
  is_hdr = out in {"smpte2084", "arib-std-b67"}  # PQ or HLG
  ```
  When HDR, add this filter (same chain Jellyfin uses for software tone-mapping):
  ```
  -vf zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable,zscale=t=bt709:m=bt709:r=tv,format=yuv420p
  ```
  Apply conditionally — for SDR sources this filter is wasted CPU and
  `tonemap` has undefined behavior on non-HDR input.

## Implementation outline

```python
# transcribe/hls_precompute.py

import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

CACHE_ROOT = Path("/app/data/hls_cache")
hls_executor = ThreadPoolExecutor(max_workers=1)  # one transcode at a time
_hls_jobs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


def _cache_dir_for(video: Path) -> Path:
    wrapper = video.relative_to("/bt").parts[0]
    return CACHE_ROOT / wrapper / video.stem


def is_complete(cache_dir: Path) -> bool:
    m = cache_dir / "master.m3u8"
    if not m.exists():
        return False
    return b"#EXT-X-ENDLIST" in m.read_bytes()


def is_in_flight(cache_dir: Path) -> bool:
    key = str(cache_dir)
    with _lock:
        proc = _hls_jobs.get(key)
    return proc is not None and proc.poll() is None


def transcode(video: Path) -> None:
    """Run synchronously in the executor thread."""
    cache_dir = _cache_dir_for(video)
    cache_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-b:v", "8M", "-profile:v", "high", "-level", "4.1",
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(cache_dir / "seg_%d.ts"),
        str(cache_dir / "master.m3u8"),
    ]
    proc = subprocess.Popen(args, stderr=subprocess.PIPE)
    key = str(cache_dir)
    with _lock:
        _hls_jobs[key] = proc
    try:
        proc.wait()
    finally:
        with _lock:
            _hls_jobs.pop(key, None)


def queue_pending() -> None:
    """Called from main.py's bt scan loop. Walk filtered wrappers,
    queue any videos whose cache isn't complete and isn't in flight.
    Debris (no ENDLIST + no live proc) is rm-rf'd first."""
    import shutil
    for wrapper in Path("/bt").iterdir():
        if not (wrapper / ".filtered").exists():
            continue
        for video in wrapper.iterdir():
            if video.suffix.lower() not in {".mkv", ".mp4", ".ts", ".avi", ".mov", ".webm"}:
                continue
            cache_dir = _cache_dir_for(video)
            if is_complete(cache_dir):
                continue
            if is_in_flight(cache_dir):
                continue
            if cache_dir.exists():
                shutil.rmtree(cache_dir)  # debris
            hls_executor.submit(transcode, video)
```

Plus 3 endpoints in main.py:
- `GET /api/hls/cache/{wrapper}/{stem}/{filename}` → `FileResponse` from cache (404 if not there)
- `GET /api/hls/progress?path=...` → read `progress.json`
- `POST /api/hls/progress` → write `progress.json`

## Progress storage

`data/progress.json`:

```json
{
  "/bt/The.Sopranos.S05.../E01.mkv": {
    "position_seconds": 412.7,
    "updated_at": "2026-06-26T15:42:01Z"
  }
}
```

- Write on every progress beat (1 s cadence, same as today)
- Read on `/api/play/resolve` to compute `resume_at_seconds`
- Atomic via temp-file + `os.replace`
- JSON is fine until ~10k entries — switch to SQLite if it ever matters

## What changes in transcribe codebase

**Remove**:
- `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `_jellyfin_user_id` and related env in `docker-compose.yml`
- `_jellyfin_index`, `_refresh_jellyfin_index`, `_resolve_item_id`, `_transcribe_path_to_jellyfin_path`
- The httpx-based Jellyfin proxy in `play_proxy`
- The `_jellyfin_index_lock`, `_jellyfin_index_at` machinery
- Jellyfin startup user-id lookup in `lifespan()`

**Add**:
- New module `transcribe/hls_precompute.py` (≈100 lines, see outline above)
- New background loop in `lifespan()` calling `hls_precompute.queue_pending()` every 30 s
- `/api/hls/...` endpoints in `main.py`
- `progress.json` read/write helpers
- ffmpeg in `transcribe/Dockerfile` if not already there

**Refactor**:
- `/api/play/resolve` now reads progress.json + computes the cache URL
- `/api/play/progress` writes progress.json
- Subtitle endpoint `/api/play/sub` unchanged (sidecar SRT route already independent)

**Frontend stays mostly the same**:
- `resolvePlay(path)` returns the same shape; `master_url` just points elsewhere
- `reportProgress(...)` payload unchanged
- `Bt.jsx` PlayerModal unchanged

## Migration steps (when implementing)

1. Add ffmpeg to transcribe's Dockerfile if missing
2. Implement `hls_precompute.py` + the new endpoints + progress.json
3. Wire into `lifespan()` background loop
4. Test against `data/bt/The.Sopranos.S05.../E01.mkv` (HEVC 10-bit — already validated the ffmpeg argv works on this)
5. Leave Jellyfin running side-by-side at first; verify HLS cache fills + plays
6. Once verified: rip Jellyfin client code from `main.py`, `docker compose down jellyfin`, remove env vars
7. Delete `jellyfin/` from the repo
8. Update root `README.md` table (remove jellyfin row)
9. Update `transcribe/README.md` if it has a Jellyfin section

## Risks (calibrated)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ffmpeg HLS args have an edge case on a specific BT release | Medium | One bad file, easy to triage | Log + manual delete + retry |
| HEVC 10-bit input without `-pix_fmt yuv420p` (already hit once) | Certain | Encoder errors | Always include the flag |
| Disk fills if cache cap not added | Low (you have 502 G free) | Disk pressure | Add LRU GC if cache > 200 G (optional) |
| ffmpeg crashes mid-transcode | Low | Partial dir | Registry-based debris detection handles this |
| Container restart loses in-flight transcode progress | Certain on restart | Wasted CPU | Re-queue from scratch; idempotent |
| User clicks ▸ on a video that isn't fully transcoded yet | Medium during first hours after download | Need to handle "not ready" UX | Frontend shows "preparing playback" if `master.m3u8` lacks ENDLIST |

## "Not ready" UX

`/api/play/resolve` for a video whose cache isn't `is_complete()` should
return a flag like `{ready: false, eta_seconds: <derived from ffmpeg progress>}` so the modal can show "preparing — N% transcoded".

Two options for partial-cache playback:
1. **Block** — show "preparing" until ENDLIST appears; cleanest UX
2. **Stream as ready** — let the modal request `master.m3u8` even
   without ENDLIST; hls.js will treat as a live stream (no scrubber
   bar). The user can watch from start while transcode runs ahead.

Option 1 is the simpler implementation. Pick that for first cut.

## Estimated total

- ffmpeg argv validated (done in prior session — Sopranos S05E01 transcodes cleanly)
- Implement + wire + test: **half day to a day** of focused work
- Migrate off Jellyfin + verify: **half day**

Net effort: ~1 day. No "1.5" or "2" phase to chase.

## Out of scope (worth noting why)

- **HW accel (NVENC)**: host has NVIDIA GPU. Adding NVENC is ~half a day:
  swap ffmpeg for `jellyfin-ffmpeg` binary (NVENC-enabled), add GPU
  reservation in compose, change argv to `-hwaccel cuda
  -hwaccel_output_format cuda -c:v h264_nvenc -preset p4`. Yields ~3-5x
  speedup (1080p HEVC: ~10 min → ~3 min per episode). **HDR sources stay
  on the CPU path** — `tonemap_cuda` adds complexity for a workload we
  rarely hit; the `is_hdr()` branch chooses CPU vs GPU. Only add when
  queue wait actually hurts (e.g., user wants to watch something that's
  4+ hours deep in the queue). NVENC's encode ASIC is hardware-independent
  of CUDA cores, so it can run alongside whisper without contention.
- **Multi-audio track UI**: rare in BT releases. Pick first audio track; if user hits a release with directors-commentary-as-track-0, add `-map 0:a:1` manually.
- **Live transcoding fallback**: by deciding to pre-compute everything, we accept "click ▸ before transcode finishes = wait for it". Don't reintroduce live transcoding.
- **Adaptive bitrate (ABR)**: LAN-only playback doesn't need it.
- **Multiple HLS renditions**: same reason.
