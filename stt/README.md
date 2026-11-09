# stt

Upload an audio file → get a diarized transcript + ready-to-paste LLM correction prompt.

Backend is **ElevenLabs Scribe v2**. History: started as `deepgram-transcribe` (Deepgram-backed), migrated backend to ElevenLabs (2026-11), then renamed the whole tool to `stt` since the old name named a vendor that's no longer in use. Directory, docker service/container name, and on-page title are now all `stt`, in sync.

Thin web wrapper (FastAPI + one static HTML file) around `transcribe.py`, which is also standalone runnable via `uv`.

## Three tabs

- **Fri seminar** — the original flow, renamed for its actual recurring use case. Upload → pick 中文/English → diarized transcript + LLM correction prompt. Speaker count always auto-detected.
- **Meeting** — same pipeline, plus an optional "number of speakers" field. Leave blank to auto-detect (same as Fri seminar); set it if you know the exact headcount — helps Scribe separate speakers who sound similar. Capped at 32 (Scribe's own ceiling).
- **Dictate** — click the mic button to record, click again to stop, it auto-uploads and transcribes. No diarization, no correction prompt — just the text, like the voice-input button in the ChatGPT/Claude/Gemini apps. Built for the "it's just me talking" case. Browser-side capture matches `utilities/keyboard`'s push-to-talk (`MediaRecorder`, `audio/webm;codecs=opus`), except click-to-toggle instead of hold-to-talk since a dictated note can run longer than a single held phrase.

Each tab has its own results panel — running one tab never overwrites another's last output, since they track unrelated recurring use cases.

## Setup

Get an API key at https://elevenlabs.io/app/settings/api-keys. Export in the shell that runs docker compose:

```bash
export ELEVENLABS_API_KEY='<your key>'
```

## Deploy

```bash
docker compose up -d --build
```

Access via the cloudflared tunnel subdomain. **If migrating from the old `deepgram-transcribe` name**: the public subdomain itself doesn't need to change, but the Cloudflare Zero Trust dashboard's Public Hostname route for it points at `http://deepgram-transcribe:8000` — go to Networks → Tunnels → (this tunnel) → Public Hostname → edit that route's Service URL to `http://stt:8000`. This has to be done manually in the CF dashboard; nothing in this repo encodes that mapping (tunnel is `TUNNEL_TOKEN`-based, remote-managed).

## Use

**Fri seminar / Meeting** (file upload):

1. Open the web UI, pick the tab
2. Click / drop an audio file (m4a, mp3, wav, opus, ogg, flac, webm — up to 100 MB, matches CF free tier body cap)
3. Pick language (中文 / English) — controls both Scribe's recognition and the language of the LLM correction prompt + speaker label
4. (Meeting tab only) optionally type the exact speaker count
5. Click **Transcribe** (10-60s depending on length — longer files risk hitting Cloudflare's own request timeout since this is a synchronous call; see "Known limitation" below)
6. Click **Copy** on the LLM Correction Prompt card → paste into Claude / ChatGPT / whatever

Raw transcript has its own copy button too if you don't want the prompt wrapper.

**Dictate** (mic recording):

1. Open the Dictate tab, pick language
2. Click the mic button — browser asks for mic permission the first time
3. Talk. Click the mic button again to stop
4. It uploads + transcribes automatically; plain text appears below, click **Copy**

Needs a secure context (`https://` or `localhost`) for `getUserMedia` to work at all — fine via the cloudflared tunnel, won't work over a bare `http://` LAN IP.

## Chinese output (Traditional vs Simplified)

ElevenLabs' `language_code` only accepts generic `zh` — there's no `zh-TW`/`zh-CN` variant at the API level, so Scribe's output script isn't guaranteed. `transcribe.py` runs every 中文 transcript through [OpenCC](https://github.com/BYVoid/OpenCC) (`s2t` config) unconditionally before returning it — a no-op if Scribe already emitted Traditional, a fix if it emitted Simplified. This keeps the 中文 button's output stable regardless of what the model actually produces.

## Known limitation: long recordings may time out

Both endpoints block synchronously on ElevenLabs' response. For a long recording (e.g. a 90-minute seminar), two things can time it out before a result ever comes back:

1. Scribe's own processing time scales with audio length
2. Cloudflare Tunnel's free-tier request timeout (~100s) applies per-request

ElevenLabs' API supports an async `webhook` mode (returns 202 immediately, calls back on completion) that would fix this properly, but it needs a second endpoint here to receive the callback — not wired up yet. If long recordings start failing, that's the fix.

## CLI fallback

`transcribe.py` runs standalone without the server, via `uv`:

```bash
uv run transcribe.py my_audio.m4a                    # zh-TW, auto speaker count
uv run transcribe.py my_audio.m4a en                 # English
uv run transcribe.py my_audio.m4a zh-TW 4             # zh-TW, exactly 4 speakers
```

Writes `my_audio_correction_prompt.txt` next to the input. Same `ELEVENLABS_API_KEY` env var.

## Model + language

Hardcoded to ElevenLabs `scribe_v2` in `transcribe.py` (`STT_MODEL`). Two languages exposed: `zh-TW` (default) and `en`. Adding another language: extend `LANGUAGES` in `transcribe.py` (needs an ElevenLabs `language_code` + a speaker-segment label), add a case in `build_correction_prompt`, and add a button to both `.lang-select` blocks in `index.html`.

## Parameters not currently wired up (ElevenLabs Scribe v2 supports these)

- **`keyterms`** — bias list of proper nouns / jargon (up to 1000 terms, 50 chars each). Highest-value addition not yet made: fill in names/terms specific to a seminar or meeting before transcribing, accuracy on them goes up noticeably.
- **`use_multi_channel`** — if a recording has one speaker per audio channel (e.g. a Zoom per-participant export), this diarizes by channel instead of ML-guessing — much more accurate when available.
- **`tag_audio_events`** — currently forced `False`. If enabled, tags laughter/applause/etc. inline as `(laughter)` — would need UI + correction-prompt handling to not look like noise.
- **`diarization_threshold`** (0.1–0.4, only applies when `num_speakers` is unset) — tunes auto-detect sensitivity. Not exposed; default has been fine so far.
- **`source_url`** — transcribe directly from a URL (including a Zoom cloud recording link) instead of uploading. Convenient, but means the server fetches an arbitrary URL you paste in — fine for a single-user tool, worth a second thought before ever exposing this to more than one trusted user.
