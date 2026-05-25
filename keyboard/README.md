# keyboard

Push-to-talk voice input PWA. Hold the button, speak (50-char-ish utterances), release — get a cleaned-up transcript ready to copy. Works on iPhone Safari (Add to Home Screen) and any desktop Chrome / Safari, all over a single Cloudflare Tunnel.

Stack: faster-whisper (CUDA) → small LLM correction (Ollama) → FastAPI proxy that also serves the static PWA. No build step.

## Architecture

```
PWA (browser)
  │   HTTPS via Cloudflare Tunnel
  ▼
keyboard-backend (FastAPI on 8080)
  │  ┌────────────────────────────────┐
  │  │ /             → index.html     │
  │  │ /style.css /app.js /…          │
  │  │ /api/transcribe                │
  │  │ /api/corrections (GET/PUT)     │
  │  └────────────────────────────────┘
  │
  ├──► keyboard-whisper          (in compose, on my_network)
  └──► ollama (external)         (separate compose, on my_network)
```

`my_network` is a shared Docker network used across multiple personal services (cloudflared, ollama). It must already exist; this repo doesn't create it.

## Pipeline (per recording)

1. Browser records `audio/webm;codecs=opus` while the button is held, with a live waveform driven by Web Audio `AnalyserNode`.
2. On release, blob is `POST`ed to `/api/transcribe`.
3. Backend forwards to whisper (`/v1/audio/transcriptions`) — language auto-detected, bilingual `initial_prompt`.
4. Whisper raw text → LLM correction via Ollama (`/api/generate`, `stream:false`).
5. LLM prompt embeds the user's vocabulary list (`corrections.json`) as a hard override so the model rewrites mishearings to the canonical form.
6. Response: `{raw, final, timing: {whisper_ms, llm_ms}}`. UI shows both raw and final in two columns, each with its own copy button.

## Models

- **Whisper**: `deepdml/faster-whisper-large-v3-turbo-ct2` (set in `docker-compose.yml`, downloaded once into the `whisper-models` named volume on first request).
- **LLM**: `qwen3:8b` on Ollama. Previously `gemma3:12b` — switched to qwen3 for ~half the VRAM footprint (≈5 GB vs ≈9 GB), which removes cross-container OOM pressure when whisper jobs run concurrently on the same GPU. Earlier qwen versions leaked simplified Chinese into zh-Hant output; qwen3 handles zh-Hant cleanly in practice. If zh-Hans leakage reappears, swap back via the `LLM_MODEL` env var.
- LLM model is configurable via `LLM_MODEL` env var in compose. Pull with `ollama pull <model>` first.

## Layout

```
keyboard/
├── docker-compose.yml
├── backend/
│   ├── Dockerfile          # python:3.12-slim + uvicorn
│   ├── requirements.txt    # fastapi, uvicorn, httpx, python-multipart
│   ├── main.py             # FastAPI app, whisper+ollama proxy, static mount
│   └── data/
│       └── corrections.json   # vocabulary overrides; bind-mounted, persists across restarts
└── frontend/
    ├── index.html          # bind-mounted into the backend container
    ├── style.css
    ├── app.js
    └── manifest.json       # PWA manifest, emoji ⌨️ icon
```

## Run

Prerequisites:
- Docker with NVIDIA GPU support (RTX 3060 here).
- External Docker network `my_network` already exists.
- An Ollama instance reachable as `ollama:11434` on `my_network`, with the LLM model already pulled. **Ollama does not auto-download** — pull it manually first:
  ```sh
  ollama pull qwen3:8b
  # or, if you don't have the CLI handy:
  curl -X POST http://<ollama-host>:11434/api/pull -d '{"model":"qwen3:8b"}'
  ```
- A Cloudflare Tunnel pointed at `backend:8080` on `my_network` (also external, not in this compose).

```sh
docker compose up -d --build
```

The Whisper model **does** auto-download — first push-to-talk pulls `~1.5 GB` from HuggingFace into the `whisper-models` named volume (~30–120s). Subsequent cold starts read it from the volume. The model auto-offloads after ~5 min idle and reloads on next request; the backend's whisper timeout is 300s to absorb that.

## Vocabulary list

Open the gear icon → 詞彙表. Each entry has a canonical form and a list of common mishearings. They get embedded into the LLM prompt as a "MANDATORY VOCABULARY OVERRIDES" block, e.g.:

```
- rewrite 'Cloud code' → 'Claude code'
```

Saved to `backend/data/corrections.json` via atomic write (tmp + `os.replace`).

## Frontend notes

- Push-to-talk uses `pointerdown`/`pointerup` with `setPointerCapture`, so a finger drifting off the button doesn't cancel the recording.
- A fresh `MediaStream` is acquired per recording (and tracks stopped on release) — reusing a cached stream across `MediaRecorder` instances produces malformed WebM on iOS Safari's second take.
- `mediaRecorder.start(250)` chunks the audio every 250 ms; combined with `requestData()` before `stop()`, this avoids a final-flush corruption mode.
- HTML response is `no-store`; CSS/JS use mtime cache-busters auto-injected by the `/` route in the backend, so file edits propagate without manual version bumps.
- Frontend is bind-mounted (`./frontend:/app/frontend:ro`) — edits are live, no rebuild needed.

## API

| Method | Path                | Body                  | Returns                                          |
| ------ | ------------------- | --------------------- | ------------------------------------------------ |
| POST   | `/api/transcribe`   | `multipart` `audio`   | `{raw, final, timing: {whisper_ms, llm_ms}}`     |
| GET    | `/api/corrections`  | —                     | `{<canonical>: [<mishearing>, …], …}`            |
| PUT    | `/api/corrections`  | same shape as GET     | `{ok: true}`, persists to disk and reloads       |

## Latency (warm, RTX 3060, qwen3:8b)

- Whisper (≤2s of audio): ~150–300 ms
- LLM correction: ~600–1200 ms
- Total round-trip incl. network: ~1–1.5 s

Cold start (model load): +30–120 s on first request after restart or 5 min idle.

## Known small things

- `Cache-Control` on `/style.css` and `/app.js` falls back to browser heuristic caching because Starlette's `StaticFiles` doesn't send a header. The `/` route works around this by appending `?v=<mtime>` so a content change becomes a new URL. The cleaner long-term fix is a `StaticFiles` subclass that sets `Cache-Control: no-cache` (revalidate via `Last-Modified`/`ETag`).
- `_corrections` is module-level mutable state in `main.py`; load/save mutate it. Fine for a single-process app, would need locking if scaled.
