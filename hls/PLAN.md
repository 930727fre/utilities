# hls — Jellyfin replacement for transcribe playback

A thin Python service that gives transcribe the three things it currently
borrows Jellyfin for, **without the ~2 GB Jellyfin container** and its
60+ moving parts we don't use.

This file is a spec for a follow-up session. Read it cold — assume zero
context from the prior conversation.

## Goal

transcribe's bt-tab ▸ button currently:

1. POST `/api/play/resolve` (transcribe) → looks up Jellyfin item id by file path
2. GET `/api/play/proxy/{item_id}/master.m3u8` (transcribe → Jellyfin HLS)
3. POST `/api/play/progress` (transcribe → Jellyfin UserData write) + read on resolve

We want the same browser playback + cross-session resume but with
Jellyfin replaced by a small in-house service.

## Non-goals (explicit)

- No Apple TV / iOS native Jellyfin app compatibility (user uses Infuse on iOS, SMB+IINA on Mac)
- No multi-user / auth (single-user)
- No Jellyfin REST API surface compatibility
- No DLNA / Chromecast / DVR / live TV
- No library scanner / metadata scraping (TheTVDB, TMDB)
- No web UI of its own — UI lives in transcribe
- No HW acceleration on first iteration (CPU software encode only)
- No adaptive bitrate (single HLS rendition)

## Architecture decision

- **Python + FastAPI**, lives inside transcribe-app OR as separate
  `hls/` service on `my_network`. Recommendation: inside transcribe-app
  for v1 (less moving parts, shared volumes already mounted). Split into
  its own service later if it gets big.
- **ffmpeg via subprocess**, no python-ffmpeg wrapper needed (plain
  `subprocess.Popen` is fine)
- **Always transcode** to h264 + AAC 2ch — no codec compatibility
  detection. ffmpeg handles every input we'll see in BT releases
  (HEVC / x264 / VP9 / AV1 / AC3 / DTS / FLAC / etc.). CPU cost is the
  trade-off and we accept it (single-user, host CPU sits idle most of
  the time).
- **State**: in-memory `dict[session_id → Session]` + a `progress.json`
  file persisted to transcribe's data dir.

## Scope: 5 endpoints to deliver

Replacing transcribe's current 5 Jellyfin calls:

| transcribe needs | new endpoint | replaces |
|------------------|--------------|----------|
| video path → resolvable identifier | (none — use the path directly as session key) | `GET /Users` + `GET /Items` index |
| read last position | `GET /api/hls/progress?path=...` | `GET /Users/{u}/Items/{i}` |
| write current position | `POST /api/hls/progress` | `POST /UserItems/{i}/UserData` |
| HLS master playlist | `GET /api/hls/{session}/master.m3u8` | `GET /Videos/{i}/master.m3u8` |
| HLS segments + sub-playlist | `GET /api/hls/{session}/{seg}.{ts,m3u8}` | proxy chain under `/Videos/{i}/...` |

`session_id` is a short uuid generated server-side when transcribe
calls a new `POST /api/hls/start` with `{path, resume_at_seconds}`.
Sessions are 1:1 with browser modal opens.

## Key patterns to lift from Jellyfin source (NO verbatim copy — clean-room rewrite)

Read these four files in the Jellyfin 10.11.x release tag:

| Jellyfin file | What to look at |
|---------------|------------------|
| `Jellyfin.Api/Controllers/DynamicHlsController.cs` | The HLS endpoint orchestration + the "**absolute segment numbering**" trick (segment N corresponds to a fixed point in the source video, regardless of which transcoding job is serving it) |
| `MediaBrowser.Controller/MediaEncoding/EncodingHelper.cs` (~6000 lines, skim) | The ffmpeg argv they build for "transcode HEVC/h264 source to h264 HLS". Pay attention to: `-ss`, `-noaccurate_seek`, `-vsync`, `-avoid_negative_ts`, the HLS-specific flags (`-hls_flags +independent_segments`, `-hls_segment_type mpegts`, `-hls_time`, `-hls_playlist_type vod`) |
| `Jellyfin.Api/Helpers/TranscodingJobHelper.cs` | Job lifecycle: tmpdir per session, kill-on-idle timer, segment cleanup, the "transcode-ahead but not too far" throttling |
| `MediaBrowser.MediaEncoding/Encoder/EncoderValidator.cs` | NOT for codec selection (we always transcode), but for confirming ffmpeg supports the args we want |

**License**: Jellyfin is GPL-2.0. Don't copy code verbatim. Read for patterns, then write fresh.

## The "absolute segment numbering" pattern (the hard part)

This is the most non-obvious thing Jellyfin does. Worth 80% of the
focus when porting.

Naive approach (the wrong one):
- Spawn ffmpeg from seek time, segments are numbered 0..N from the seek
- User scrubs to a new position → spawn new ffmpeg, segments restart at 0
- hls.js sees discontinuous sequence → choke

Jellyfin approach:
- The HLS playlist references segments by their **absolute position in the source video**:
  `segment_<absolute_index>.ts` where `absolute_index = floor(source_time / segment_duration)`
- A 90-minute source at 6 s/segment → segments 0 through 899, always
- When a segment is requested:
  - Compute `requested_source_time = absolute_index * segment_duration`
  - Is there a live ffmpeg whose output range covers this time? Serve from there
  - If not, kill the old ffmpeg, spawn a new one from `-ss requested_source_time`, wait for that segment to appear, serve it
- Client (hls.js) sees a continuous timeline; backend invisibly switches transcoding jobs underneath

Implementation notes:
- ffmpeg's `-hls_segment_filename '%d.ts'` numbers segments starting at 0 per-process. We renumber the files (rename or symlink) so the absolute index matches.
- Or simpler: write segments to a temp file then rename to the absolute name once ready.
- The master playlist is static (we know source duration via ffprobe).
- A "current job" tracks: `{start_source_time, last_segment_written, ffmpeg_proc, tmpdir}`.

## Implementation phases

### v1 — no scrubber seek

- `POST /api/hls/start {path, resume_at_seconds}` → spawns ffmpeg with
  `-ss resume_at_seconds`, returns `session_id` + master.m3u8 URL
- Segments numbered starting at 0 from the resume position (not absolute)
- Master playlist runs from 0 to (source_duration - resume_at_seconds)
- If user scrubs the seek bar in the browser → hls.js will buffer-and-skip,
  meaning either a long wait or playback stalls. Acceptable for v1.
- ~200 lines. 1-2 days.

### v1.5 — scrubber seek with absolute segment numbering

- Implement the pattern above. Master.m3u8 declares all segments 0..N
  based on full source duration.
- Session has a list of ffmpeg jobs; on segment request, dispatch to the
  job that covers it OR spawn a new one.
- Old jobs killed when superseded or idle > threshold.
- +150 lines. +2-3 days. Higher risk of edge cases.

### v2 (optional, only if needed)

- HW accel detection + nvenc / vaapi paths (jellyfin-ffmpeg patches are
  the reference; our host has NVIDIA so nvenc is feasible)
- Multiple audio track selection (UI-driven from the modal)
- Adaptive bitrate (multiple renditions in master.m3u8)

## Progress storage

`data/progress.json` in transcribe's data dir:

```json
{
  "/bt/The.Sopranos.S05.../E01.mkv": {
    "position_seconds": 412.7,
    "duration_seconds": 3132.4,
    "updated_at": "2026-06-26T15:42:01Z"
  },
  ...
}
```

- Write on every progress beat from frontend (same 1s cadence we have now)
- Read on `POST /api/hls/start` to pass `resume_at_seconds`
- Atomic write via temp-file + rename
- Don't bother with a DB for v1; if we grow past a few thousand entries
  switch to SQLite

## What changes in transcribe codebase

**Remove**:
- `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `_jellyfin_user_id` and the related env vars in `docker-compose.yml`
- `_jellyfin_index`, `_refresh_jellyfin_index`, `_resolve_item_id`, `_transcribe_path_to_jellyfin_path`
- `play_resolve` body changes drastically — calls into our new in-process module instead of Jellyfin
- `play_proxy` endpoint is replaced by the new `/api/hls/...` endpoints
- `play_progress` endpoint targets `progress.json`, not Jellyfin

**Add**:
- New module: `transcribe/hls.py` with the FastAPI sub-router + Session class
- Background task in `lifespan()` for idle session cleanup
- ffmpeg + ffprobe must be in transcribe's Dockerfile (check what's there; might already be present for whisper-related work)

**Frontend stays mostly the same**:
- `resolvePlay(path)` returns the same shape `{master_url, subtitles, resume_at_seconds}`
- `reportProgress(...)` keeps firing — same payload
- The HLS URL just lives at `/api/hls/{session}/master.m3u8` instead of `/api/play/proxy/...`

## Migration steps (when implementing)

1. Read Jellyfin 10.11.x source (3-4 hours): the 4 files listed above
2. Implement `transcribe/hls.py` with v1 scope. Test against
   `data/bt/The.Sopranos.S05.../E01.mkv` (HEVC source — the hard case)
3. Wire transcribe endpoints to new module (keep old Jellyfin code
   path-untouched in a feature branch until v1 verified)
4. Test end-to-end via transcribe browser modal
5. If clean: rip out Jellyfin client code, `docker compose down` the
   jellyfin/, delete the env vars
6. v1.5 in a follow-up

## Risks (calibrated)

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ffmpeg HLS args have a subtle quirk that breaks Safari native HLS | Medium | Annoying | Test in Safari first; reference Jellyfin's argv |
| Resume at X has ≤ 2s drift (keyframe snap) | High | Acceptable | Document; user shouldn't notice |
| ffmpeg dies mid-stream silently | Low | Bad UX | Sentinel: if no new segment in 30s + proc dead → 500 |
| Disk fill from leaked tmpdirs | Medium | Bad | Cleanup loop + hard cap on session count |
| Absolute segment numbering edge case (segment requested mid-spawn) | Medium | Bug | Job map + wait-for-segment + 30s timeout |
| Audio sync drift on long videos | Low | Annoying | Use `-vsync cfr` |
| HEVC 10-bit source breaks ffmpeg's libx264 output | Low | Single-file bug | `-pix_fmt yuv420p` forces 8-bit output |

## Out of scope for the spec — but useful background

- The host has NVIDIA GPU (used by whisper via gpu-broker). NVENC for
  Jellyfin would have meant coordinating GPU access. CPU software encode
  side-steps that entirely. Reconsider HW accel only if CPU encode is
  visibly slow.
- The transcribe annotation pipeline is unrelated to playback — don't
  conflate. SRT serving for `<track>` lives in `play_sub` and stays.
- macOS / iOS browsers play HLS natively; Chrome/Firefox use hls.js.
  Already handled in current frontend.

## Estimated total

- v1: 1-2 days focused work (read Jellyfin half day + implement day +
  test half day)
- v1.5: +2-3 days
- v2: only if specific need arises

## When done — what to commit

- `hls/` is empty after this spec lands (transcribe-internal module)
- Add new `transcribe/hls.py`
- Edit `transcribe/main.py`, `docker-compose.yml`
- Delete `jellyfin/` directory (`rm -rf` after `docker compose down`)
- Update root `README.md` table (remove jellyfin row)
- Update `transcribe/README.md` if there's a playback section
