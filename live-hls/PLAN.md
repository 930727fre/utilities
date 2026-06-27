# live-hls — standalone live HLS transcoder

Independent FastAPI + ffmpeg service. Point at any video file on a mounted
read-only volume, play it in a browser via on-demand NVENC transcoding to
HLS. Replicates the core of Jellyfin's `DynamicHlsController` flow without
its 8000+ lines of multi-client / multi-codec / DLNA / scanner baggage.

Built as a standalone tool so we can iterate on the seek + lifecycle logic
in isolation, then integrate into `transcribe` once stable.

Read this cold — assume zero prior conversation.

## Goal

Browser opens a player → picks a video path → live transcode starts →
playback proceeds → user can seek anywhere → no pre-compute, no cache, no
disk pressure.

## Non-goals (explicit)

- Subtitle handling (separate sidecar mount in the real integration; this
  tool is for transcode only)
- Cross-device progress sync
- Multi-user / auth
- Adaptive bitrate
- HDR tone-mapping (CPU fallback can be ported later; v1 is SDR/NVENC only)
- Persistent state across container restart

## Architecture

```
Browser
  │
  ├─ POST /api/start {path}
  │    ← {session_id, master_url, duration_seconds}
  │
  ├─ GET /api/{sid}/master.m3u8
  │    ← static playlist listing all seg_0..seg_(N-1).ts
  │
  ├─ GET /api/{sid}/seg_N.ts        ← happens N times during playback
  │    ┌─ on disk? serve immediately
  │    ├─ in-flight ffmpeg near N?  wait for the file to appear
  │    ├─ in-flight too far ahead or behind? kill, respawn from N
  │    └─ no ffmpeg? spawn from N
  │
  └─ DELETE /api/{sid}              ← modal close cleanup
```

Each session owns a temp directory `sessions/<sid>/`. ffmpeg writes
`seg_*.ts` into it. Master playlist is generated up-front from source
duration (via ffprobe) — VOD style, no real-time playlist updates.

## State per session

```python
@dataclass
class HlsSession:
    sid: str                          # uuid hex, 8 chars
    source_path: Path                 # validated against allowed mount root
    duration_seconds: float           # from ffprobe at session start
    segment_length: int = 6           # constant for v1
    work_dir: Path                    # sessions/<sid>/
    proc: subprocess.Popen | None     # current ffmpeg, None when idle
    proc_start_seg: int               # which absolute seg index this proc starts at
    last_request_at: float            # unix ts; used for idle GC
    lock: threading.Lock              # serializes respawn decisions
```

In-memory `_sessions: dict[str, HlsSession]`. Container restart clears
everything (sessions are by definition ephemeral).

## Absolute segment numbering (the seek trick)

ffmpeg by default numbers segments from 0 per process. Naive: every seek
spawns a new ffmpeg writing seg_0, seg_1... → conflicts with the master
playlist that already declared seg_N at fixed positions.

Jellyfin's fix (replicated here):
- Master playlist lists segments by their **source-time position**:
  seg_K corresponds to source time `[K * segment_length, (K+1) * segment_length)`.
  So for a 50-min source at 6s segments, master.m3u8 lists seg_0..seg_499.
- When ffmpeg is launched from source time `T`, give it `-start_number {T / 6}`
  so its output is named `seg_{T/6}.ts, seg_{T/6+1}.ts, ...`. Player sees a
  continuous, consistent numbering.

ffmpeg argv:
```
ffmpeg -y \
  -ss {start_seconds} \
  -hwaccel cuda -hwaccel_output_format cuda \
  -i {source} \
  -vf scale_cuda=format=nv12 \
  -c:v h264_nvenc -preset p4 -b:v 8M -profile:v high -level 4.1 \
  -c:a aac -b:a 192k -ac 2 \
  -copyts -avoid_negative_ts disabled \
  -f hls -hls_time 6 -hls_list_size 0 -hls_playlist_type vod \
  -hls_segment_filename {work_dir}/seg_%d.ts \
  -start_number {start_seg_index} \
  {work_dir}/internal.m3u8
```

Notes:
- `-copyts` + `-avoid_negative_ts disabled` keep timestamps coherent across
  respawns so player doesn't see gaps.
- We never serve `internal.m3u8` — clients always get the pre-generated
  master.m3u8. Internal playlist is just where ffmpeg writes its growing
  segment list (we only need the .ts files).
- `-vf scale_cuda=format=nv12` coerces 10-bit HEVC down to 8-bit (NVENC
  doesn't emit 10-bit h264).

## Segment request flow (replicates DynamicHlsController.GetDynamicSegment)

```python
def serve_segment(sid: str, seg: int) -> Response:
    s = _sessions[sid]
    s.last_request_at = time.time()
    seg_path = s.work_dir / f"seg_{seg}.ts"

    # Fast path
    if seg_path.exists():
        return FileResponse(seg_path)

    with s.lock:
        # Re-check under lock
        if seg_path.exists():
            return FileResponse(seg_path)

        current_idx = _current_ffmpeg_index(s)
        gap_threshold = 24 // s.segment_length  # 4 segments at 6s = ~24s ahead

        respawn = (
            s.proc is None
            or s.proc.poll() is not None      # process died
            or current_idx is None
            or seg < current_idx              # seeking backwards
            or (seg - current_idx) > gap_threshold  # seeking too far ahead
        )

        if respawn:
            _kill(s)
            _spawn(s, start_seg=seg)
        # else: ffmpeg is close to seg, just wait

    # Block until the file appears OR ffmpeg dies OR timeout
    return _wait_for_seg(s, seg_path, timeout=30)


def _current_ffmpeg_index(s: HlsSession) -> int | None:
    """Jellyfin's GetCurrentTranscodingIndex: find the most-recently
    modified seg_*.ts and parse its index."""
    if s.proc is None or s.proc.poll() is not None:
        return None
    segs = list(s.work_dir.glob("seg_*.ts"))
    if not segs:
        return None
    newest = max(segs, key=lambda p: p.stat().st_mtime)
    return int(newest.stem.removeprefix("seg_"))


def _wait_for_seg(s: HlsSession, seg_path: Path, timeout: float) -> Response:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if seg_path.exists():
            return FileResponse(seg_path)
        if s.proc is None or s.proc.poll() is not None:
            return Response(status_code=502, content="transcoder died")
        time.sleep(0.05)
    return Response(status_code=504, content="timed out waiting for segment")
```

## Lifecycle

**Session creation** (`POST /api/start {path}`):
1. Validate path is under allowed mount root (e.g. `/media`).
2. `ffprobe` for duration. Reject if can't determine.
3. Generate session uuid, mkdir work_dir.
4. Pre-compute master.m3u8 from duration + segment_length, write to disk.
5. Register session, return `{sid, master_url, duration_seconds}`.

**Session destruction**:
- Explicit: `DELETE /api/{sid}` → kill ffmpeg, rmtree work_dir, drop from registry.
- Idle GC: background task every 30s. If `last_request_at` > 60s ago → destroy.
- Container shutdown (lifespan): destroy all.

**Respawn during seek**:
1. Take session lock.
2. SIGTERM current proc, wait up to 2s for clean exit, SIGKILL.
3. Delete all `seg_*.ts` files >= new start_seg (avoid serving stale data).
4. Spawn new ffmpeg with `-ss {start_seg * 6}` + `-start_number {start_seg}`.
5. Release lock.

## Master playlist generation

For a video of duration D seconds at segment length L:
- Whole segments: `floor(D / L)`
- Last segment: `D % L` (if nonzero)

```
#EXTM3U
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:6.000000, nodesc
seg_0.ts
#EXTINF:6.000000, nodesc
seg_1.ts
...
#EXTINF:3.142000, nodesc
seg_499.ts
#EXT-X-ENDLIST
```

The segment URLs are relative — browser resolves them against the
master.m3u8 URL: `/api/{sid}/master.m3u8` → segment fetches go to
`/api/{sid}/seg_N.ts`. No query string, no API key.

## Directory layout

```
live-hls/
├── PLAN.md (this file)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py                 # FastAPI app + endpoints + lifespan
├── transcoder.py           # HlsSession class, spawn/kill/wait
├── static/
│   └── index.html          # test UI (path input + hls.js player)
└── (runtime) sessions/<sid>/
                ├── master.m3u8
                ├── internal.m3u8   # ffmpeg's own playlist, never served
                └── seg_*.ts
```

## API surface

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| POST | `/api/start` | `{path: string}` | `{sid, master_url, duration_seconds}` |
| GET | `/api/{sid}/master.m3u8` | – | playlist text |
| GET | `/api/{sid}/seg_{N}.ts` | – | segment bytes (or 502/504) |
| DELETE | `/api/{sid}` | – | `{ok: true}` |
| GET | `/health` | – | `{ok: true}` |
| GET | `/` | – | static index.html |
| GET | `/static/{file}` | – | static assets (hls.js bundled or CDN) |

## Configuration

Compose env:
- `MEDIA_ROOTS=/media`: comma-separated allowed mount paths. POST /start
  rejects paths outside these.
- `SESSION_IDLE_TIMEOUT=60`: seconds before idle GC kicks in.
- `SESSION_DIR=/tmp/live-hls`: where session work dirs live (tmpfs friendly).
- NVIDIA env mirrors transcribe-app for NVENC.

GPU device reservation in compose deploy.resources, same as transcribe-app.

## Test UI (static/index.html)

Minimal:
- Header: `live-hls test`
- Path input + Play button
- Big `<video>` element + hls.js
- Status panel: session_id, current ffmpeg pid (if any), current seg index,
  idle timer, kill button
- Polls `/api/{sid}/status` every 1s for state (this is the only extra
  endpoint not in the API table above — drop if it bloats scope)

## Risks / known edge cases

| Risk | Mitigation |
|------|------------|
| ffmpeg hangs (the 64KB stderr buffer bug we hit on pre-compute) | Use `proc.communicate()` or drain stderr in a background thread from spawn time |
| Seek during respawn race | The session lock serializes; client retries on 502 |
| Slow ffmpeg startup (~3s first segment for HEVC source) | `_wait_for_seg` 30s timeout covers it |
| `-copyts` mismatched between respawns causing player stall | Spec says it should be coherent; test in practice; fall back to `-output_ts_offset` if not |
| User opens many sessions on the same source | Each gets its own work_dir + ffmpeg. Idle GC reclaims |
| Disk fill from accumulated session dirs | Idle GC + cap on session count |
| Source file has weird PTS / VFR | Add `-fps_mode cfr` if hit |

## Implementation steps

1. Scaffold dirs + Dockerfile + compose (~30 min)
2. `transcoder.py`: HlsSession class with spawn/kill/wait helpers (~2h)
3. `main.py`: endpoints (~1h)
4. `static/index.html`: minimal UI (~30 min)
5. Test against 3-5 video sources of varying length / codec (~1h)
6. Iterate on seek behaviour (~1-2h)

Total: ~half to one day.

## After v1 verified

Integrate into transcribe:
- Replace pre-compute hls_precompute.py
- `/api/play/resolve` returns a live-hls session URL instead of a static playlist URL
- Frontend Bt.jsx PlayerModal calls /api/start on mount, /api/{sid} delete on unmount
- Subtitle endpoints stay unchanged (separate from transcode)
- Drop `data/derived/.../master.m3u8` cache — instant freedom from the disk
  cost we built up overnight

## Out of scope

- Multi-rendition ABR
- Subtitle muxing (sidecar serving stays in transcribe-app)
- Resume across reload (frontend can stash position in localStorage if useful)
- Pre-warm cache for likely-next videos
- HDR tone-mapping (port the `is_hdr` CPU path from hls_precompute later)
