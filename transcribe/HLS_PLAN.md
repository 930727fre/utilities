# transcribe pipeline refactor: decouple bt/ from derived/

The bt-tab pipeline today writes annotation / translation / playback artifacts
back into `data/bt/<wrapper>/`, mixed in with the torrent's own files. This
created the disaster where `bt_filter`'s cleanup pass deleted ~11 wrappers'
worth of derived SRTs. The new architecture decouples the two:

- `data/bt/<wrapper>/` — **read-only** after aria2 finishes. Nothing in our
  pipeline ever writes here again.
- `data/derived/<wrapper>/<stem>/` — every pipeline product lives here:
  annotated SRT, Chinese SRT, HLS playlist + segments.

The HLS pre-compute layer (which replaces Jellyfin entirely) is one of the
stages writing into `derived/`. This doc covers both the wider decoupling and
the HLS specifics.

Read this cold — assume zero prior conversation context.

## Architectural rule

Anyone touching the pipeline should keep these invariants:

1. `data/bt/` is mounted read-only. The only writer is aria2c during download.
2. `bt_filter` is a **scanner**, not a mover or deleter. It runs an LLM call
   to pair videos with English SRTs, then ensures the corresponding
   `derived/<wrapper>/<stem>/` directories exist. No `bt/` writes ever.
3. Pairing lives **in memory only**. No `manifest.json`, no symlinks. The
   wrapper-tree fingerprint is stable (BT is read-only), so the LLM gives
   the same pairing on every tick. Re-pair on every tick (~$0.001 per call).
4. Stage completion is signaled by **file presence in `derived/`**:
   `annotated.srt` exists → annotation done; `master.m3u8` ends with
   `#EXT-X-ENDLIST` → HLS done; `zh.srt` exists → translation done.

## Non-goals (explicit)

- No Apple TV / iOS native Jellyfin app compatibility (user uses Infuse on iOS, SMB+IINA on Mac — both being replaced by the browser player)
- No multi-user / auth (single-user)
- No Jellyfin REST API surface compatibility
- No DLNA / Chromecast / DVR / live TV
- No library scanner / metadata scraping
- No HW acceleration (CPU software encode only — see "Out of scope" for NVENC notes)
- No adaptive bitrate (single HLS rendition)
- No on-demand live transcoding logic — everything is pre-computed

## Derived data layout

```
data/
├── bt/<wrapper>/                              ← read-only
│   ├── Season 1/episode01.mkv
│   ├── Season 1/episode01.srt                 ← BT-bundled English (if present)
│   └── ...
└── derived/<wrapper>/<stem>/
    ├── annotated.srt                          ← whisper + Claude ※ markers
    ├── zh.srt                                  ← Gemini translation
    ├── master.m3u8                            ← HLS playlist (ENDLIST = done)
    └── seg_*.ts                                ← HLS segments
```

`<stem>` is the source video's filename without extension. Picking it
straight from the video makes the cache human-debuggable (`ls derived/`
tells you exactly which video is which).

## Pipeline integration

```
aria2 done (.aria2 gone)
    ↓
bt_filter scan tick (every 30s)
  - For each wrapper in bt/:
      LLM pair → in-memory [(stem, video_path, eng_srt_path), ...]
      For each pairing:
        mkdir -p derived/<wrapper>/<stem>/
        Dispatch missing stages (skip if file already present)
    ↓
Stages (parallel, each writing into derived/<wrapper>/<stem>/):
  ┌─ whisper + annotate → annotated.srt
  ├─ hls_precompute     → master.m3u8 + seg_*.ts
  └─ zh translate       → zh.srt  (triggered by user click on row's 中 button)
```

`bt_filter` is the **single dispatch surface**. It doesn't transcode or
annotate itself — it just identifies what work needs doing and submits
to per-stage executors. Each executor's in-memory registry handles
"in-flight" vs "queued" vs "done" so the scan tick can be idempotent.

## bt_filter (rewritten shape)

Old code did: LLM pair → flatten videos+srts to wrapper root → whitelist
delete → write `.filtered` sentinel. **All gone.** New code:

```python
def scan_and_dispatch():
    for wrapper in BT_ROOT.iterdir():
        if not download_complete(wrapper):       # .aria2 still there
            continue
        pairings = pair_wrapper_via_llm(wrapper) # [(stem, video, eng_srt), ...]
        for stem, video, eng_srt in pairings:
            derived = DERIVED_ROOT / wrapper.name / stem
            derived.mkdir(parents=True, exist_ok=True)
            dispatch_missing_stages(video, eng_srt, derived)
```

`dispatch_missing_stages` looks at `derived/` for `annotated.srt`,
`master.m3u8` (ENDLIST), `zh.srt` and only submits work for what's
missing AND not currently in flight.

Lines removed compared to old `bt_filter.py`:
- `_pipeline_siblings()`: gone (no flatten, no sibling preservation worry)
- The whitelist delete loop: gone
- The `bonus_dirs` mis-classification guard: gone (no deletion = no risk)
- `.filtered` sentinel write: gone (derived/<wrapper>/<stem>/ existence is the signal)

## HLS pre-compute: "Is it done?" — no extra sentinel

ffmpeg appends `#EXT-X-ENDLIST` to `master.m3u8` only after the entire
transcode completes. Use that as the completion marker:

```python
def is_hls_complete(derived_dir: Path) -> bool:
    m = derived_dir / "master.m3u8"
    if not m.exists():
        return False
    return b"#EXT-X-ENDLIST" in m.read_bytes()
```

No separate `.complete` file means no two-write atomicity issue.

## Crash recovery: in-memory job registries

Each stage owns its own:
```python
_hls_jobs:      dict[str, subprocess.Popen] = {}   # derived_dir → ffmpeg
_annotate_jobs: dict[str, Future]           = {}   # derived_dir → future
```

Scan tick logic per stage:

| derived state         | registry state         | action                                   |
|-----------------------|------------------------|------------------------------------------|
| stage product present | –                      | skip (done)                              |
| product missing       | in registry, alive     | skip (in flight)                         |
| product missing       | absent / proc dead     | rmtree partial debris if any, submit job |

Container restart clears registries; next tick re-dispatches whatever's
incomplete. Fully idempotent.

## ffmpeg argv (validated on macOS 2026-06-27)

Tested working on: HBO 1080p (Sopranos/Wire), modern web-dl (Chernobyl,
Spider-Verse), YIFY rip (Michael 2026). Plays cleanly in Safari native
HLS and Chrome (which also turns out to support native HLS on macOS via
AVFoundation). hls.js fallback path verified by code review only — will
be exercised in production on Linux Chrome / Firefox / mobile.

```sh
ffmpeg -y \
  -i <source.mkv> \
  -c:v libx264 -pix_fmt yuv420p \
  -b:v 8M -profile:v high -level 4.1 -preset veryfast \
  -c:a aac -b:a 192k -ac 2 \
  -f hls -hls_time 6 -hls_list_size 0 -hls_playlist_type vod \
  -hls_segment_filename '<derived_dir>/seg_%d.ts' \
  '<derived_dir>/master.m3u8'
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

import subprocess, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

DERIVED_ROOT = Path("/app/data/derived")
hls_executor = ThreadPoolExecutor(max_workers=1)
_hls_jobs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


def is_complete(derived_dir: Path) -> bool:
    m = derived_dir / "master.m3u8"
    if not m.exists():
        return False
    return b"#EXT-X-ENDLIST" in m.read_bytes()


def is_in_flight(derived_dir: Path) -> bool:
    with _lock:
        proc = _hls_jobs.get(str(derived_dir))
    return proc is not None and proc.poll() is None


def transcode(video: Path, derived_dir: Path) -> None:
    """Synchronous; call inside the executor."""
    derived_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-i", str(video),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-b:v", "8M", "-profile:v", "high", "-level", "4.1",
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(derived_dir / "seg_%d.ts"),
        str(derived_dir / "master.m3u8"),
    ]
    proc = subprocess.Popen(args, stderr=subprocess.PIPE)
    key = str(derived_dir)
    with _lock:
        _hls_jobs[key] = proc
    try:
        proc.wait()
    finally:
        with _lock:
            _hls_jobs.pop(key, None)


def ensure(video: Path, derived_dir: Path) -> None:
    """Called by bt_filter's dispatch loop. Idempotent."""
    import shutil
    if is_complete(derived_dir):
        return
    if is_in_flight(derived_dir):
        return
    # debris: derived dir exists with partial segments, no live proc
    if derived_dir.exists() and any(derived_dir.glob("seg_*.ts")):
        for f in derived_dir.glob("seg_*.ts"):
            f.unlink()
        (derived_dir / "master.m3u8").unlink(missing_ok=True)
    hls_executor.submit(transcode, video, derived_dir)
```

Endpoints in `main.py`:
- `GET /api/play/proxy/{wrapper}/{stem}/{filename}` → `FileResponse` from
  `derived/<wrapper>/<stem>/<filename>` (404 if missing)
- `GET /api/play/sub?wrapper=...&stem=...` → serve `derived/<wrapper>/<stem>/zh.srt`
  converted to VTT (or 404)
- `POST /api/play/resolve {path}` → returns `{master_url, subtitles, resume_at_seconds, ready}`
- `POST /api/play/progress` → writes `data/progress.json`

## Progress storage

`data/progress.json`:

```json
{
  "/bt/The.Sopranos.S05.../E01.mkv": {
    "position_seconds": 412.7,
    "updated_at": "2026-06-27T15:42:01Z"
  }
}
```

- Write on every progress beat (1 s cadence)
- Read on `/api/play/resolve` to compute `resume_at_seconds`
- Atomic via temp-file + `os.replace`
- JSON until ~10k entries — switch to SQLite if it ever matters

## What changes in transcribe codebase

**Remove**:
- `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `_jellyfin_user_id` env + helpers
- `_jellyfin_index`, `_refresh_jellyfin_index`, `_resolve_item_id`, `_transcribe_path_to_jellyfin_path`
- httpx-based Jellyfin proxy in `play_proxy`
- Jellyfin startup user-id lookup in `lifespan()`
- `_pipeline_siblings()` + flatten/delete in `bt_filter`
- `.filtered` sentinel reads/writes

**Add**:
- `transcribe/hls_precompute.py` (~100 lines)
- `data/derived/` as the canonical pipeline output root
- 30s scan loop in `lifespan()` driving `bt_filter.scan_and_dispatch()`
- `progress.json` read/write helpers
- ffmpeg + ffprobe in `transcribe/Dockerfile`

**Refactor**:
- `bt_filter.py`: drop flatten/delete; LLM pair only; expose `scan_and_dispatch()`
- `tasks.py` / `annotate.py`: write `annotated.srt` into `derived/<wrapper>/<stem>/`
- `translator.py`: read `derived/.../annotated.srt`, write `derived/.../zh.srt`
- `main.py` `/api/play/*`: read derived/ + progress.json (no Jellyfin)
- `main.py` `/api/bt`: walk `derived/` to compute per-episode state
- `Bt.jsx`: state from new `/api/bt`; `PlayerModal` handles `ready:false`

## Migration steps

User-driven manual migration (no migration code path — keep the codebase clean):

```bash
# For each existing wrapper that already has annotated/zh srt in bt/:
mkdir -p data/derived/<wrapper>/<stem>/
mv data/bt/<wrapper>/<stem>.zh-tw.srt           data/derived/<wrapper>/<stem>/zh.srt
mv data/bt/<wrapper>/<stem>.srt                 data/derived/<wrapper>/<stem>/annotated.srt
# (only move .srt if it contains ※; otherwise it's the BT-bundled English srt
#  and should stay in bt/ as the source for the new pipeline)
```

Implementation order:

1. Rewrite `bt_filter.py` (scanner only).
2. Update `annotate.py` / `tasks.py` output paths → `derived/`.
3. Update `translator.py` input + output paths → `derived/`.
4. Add `hls_precompute.py` + register executor + scan loop in `main.py`.
5. Refactor `main.py` play endpoints (drop Jellyfin client code entirely).
6. Refactor `main.py /api/bt` to walk `derived/` for state.
7. Update `Bt.jsx` for the new state shape + `ready:false` handling.
8. Ensure `ffmpeg` + `ffprobe` in `Dockerfile`.
9. User does the one-time manual migration above.
10. `docker compose down jellyfin`, rebuild transcribe, rebuild test on one wrapper.
11. Once verified for all wrappers: delete `jellyfin/` dir, drop Jellyfin env from compose, update root `README.md`.

## Risks (calibrated)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ffmpeg HLS args have an edge case on a specific BT release | Medium | One bad file, easy to triage | Log + manual delete + retry |
| HEVC 10-bit input without `-pix_fmt yuv420p` (already hit once) | Certain | Encoder errors | Always include the flag |
| HDR source looks washed-out (no tone-map filter) | Medium if any 4K BD HDR exists | Watchable but ugly | Add `is_hdr()` branch when first hit |
| Disk fills if cache cap not added | Low (502 G free, ~90 G estimated cache) | Disk pressure | Add LRU GC if cache > 200 G (optional) |
| ffmpeg crashes mid-transcode | Low | Partial dir | Registry-based debris detection handles this |
| Container restart loses in-flight transcode progress | Certain on restart | Wasted CPU | Re-queue from scratch; idempotent |
| User clicks ▸ before transcode finishes | Medium during the first hours after a download | Need "not ready" UX | Frontend shows "preparing playback" if `master.m3u8` lacks ENDLIST |
| bt_filter LLM mispairs srt with video | Low | Wrong subtitle on playback | Manual rm of wrong derived dir + re-pair |

## "Not ready" UX

`/api/play/resolve` for a video whose HLS isn't `is_complete()` returns
`{ready: false, eta_seconds: <derived from ffmpeg progress or null>}`.
Modal shows "preparing — transcoding".

Two options for partial-cache playback:
1. **Block** — show "preparing" until ENDLIST appears; cleanest UX.
2. **Stream as ready** — let hls.js request `master.m3u8` even without
   ENDLIST; it treats as live stream (no scrubber bar).

Option 1 for v1.

## Estimated total

- Refactor scope: ~6 hours of focused work (bt_filter rewrite, annotate / translate path changes, hls_precompute, main.py play endpoints, Bt.jsx)
- Verification: ~1 hour (rebuild, watch derived/ fill, test playback)
- Jellyfin removal: ~30 minutes (delete jellyfin/, env, README)

## Out of scope (worth noting why)

- **HW accel (NVENC)**: host has NVIDIA GPU. Adding NVENC is ~half a day:
  swap ffmpeg for `jellyfin-ffmpeg` binary (NVENC-enabled), add GPU
  reservation in compose, change argv to `-hwaccel cuda
  -hwaccel_output_format cuda -c:v h264_nvenc -preset p4`. Yields ~3-5x
  speedup (1080p HEVC: ~10 min → ~3 min per episode). **HDR sources stay
  on the CPU path** — `tonemap_cuda` adds complexity for a workload we
  rarely hit; the `is_hdr()` branch chooses CPU vs GPU. Only add when
  queue wait actually hurts. NVENC's encode ASIC is hardware-independent
  of CUDA cores, so it can run alongside whisper without contention.
- **Multi-audio track UI**: rare in BT releases. Pick first audio track;
  if a release has director's commentary as track 0, add `-map 0:a:1`
  manually.
- **Live transcoding fallback**: by pre-computing everything we accept
  "click ▸ before transcode finishes = wait for it". Don't reintroduce
  live transcoding.
- **Adaptive bitrate (ABR) / multiple HLS renditions**: LAN-only playback
  doesn't need it.
- **4K → 1080p scale**: easy to add (`-vf scale='min(1920,iw)':-2`),
  defer until a 4K source actually shows up.
- **English audio track auto-selection**: defer until a wrong track is
  picked (`-map 0:a:m:language:eng?` is the one-liner).
- **VFR audio sync (`-fps_mode cfr`)**: defer until drift observed.
- **bt_filter pairing cache**: re-pair every tick is cheap enough (~$0.03/day);
  add cache only if cost becomes meaningful.
