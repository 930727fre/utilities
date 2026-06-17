# whisper

Shared faster-whisper-server (CTranslate2 backend, `large-v3-turbo` model). HTTP API, OpenAI-compatible:

- `POST /v1/audio/transcriptions` — submit audio/video, get JSON transcript
- `GET /health` — liveness

Consumers on `my_network` reach it at `http://whisper:8000`.

## Why centralized

`transcribe`, `xyt`, and `keyboard` all need Whisper. Running a local instance per tool meant:
- 3 model loads = 3x VRAM committed
- 3 docker images carrying torch + whisper deps
- Per-tool version skew

One shared service: one VRAM footprint, lighter consumer images, single point of upgrade. faster-whisper-server queues incoming requests internally so concurrent callers serialize automatically.

## Run

```sh
docker compose up -d
```

First run downloads `deepdml/faster-whisper-large-v3-turbo-ct2` to the `whisper-models` volume (~2 GB). Subsequent starts are fast.

Consumers should check `/health` in their startup (lifespan) and fail loudly if unreachable — see `transcribe/main.py` for the pattern.
