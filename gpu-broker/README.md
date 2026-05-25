# gpu-broker

Single-machine GPU mutex + queue dashboard. Every container that touches the GPU on this host coordinates through here so they don't fight for VRAM.

## What it does

Holds at most one active lease at a time. Other requesters queue (FIFO). On release, the next waiter is woken. State is in-memory — a broker restart wipes everything and consumers fall back to "proceed without lock" until it's back.

## Stack

| Layer | Tech |
|------|------|
| Service | FastAPI on port 8000, single process |
| State | In-memory: holder + FIFO queue + last 30 jobs |
| Dashboard | One inline HTML page, auto-refreshes every 2s |
| Persistence | None — restart-clean is the design |

## API

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| `POST` | `/acquire` | `{container, workload, eta_seconds?}` | Block until GPU is free, return `{token, waited_seconds}` |
| `DELETE` | `/lease/{token}` | — | Release a held lease |
| `DELETE` | `/api/holder` | — | Admin: force-release the current holder (dashboard kill button) |
| `GET` | `/api/state` | — | JSON: `{holder, queue, history, now}` |
| `GET` | `/` | — | HTML dashboard |
| `GET` | `/health` | — | `{ok: true}` |

`POST /acquire` is a **long-poll**: the request hangs until it's the caller's turn, which can be minutes for whisper-medium queues. Clients should not set a read timeout.

## Consumers

Each consumer ships a `gpu_lock.py` helper (`requests` for sync, `httpx` for async) that wraps acquire/release. Same Python API in all four:

```python
with gpu_lock("xyt-app", "whisper:{job_id}"):
    _run_transcription(...)
```

```python
async with gpu_lock_async("keyboard-backend", "whisper"):
    await call_whisper(...)
# Gemini correction runs after release — cloud call, no GPU contention to coordinate
```

Consumers:
- `xyt-app` — whisper subprocess (translation now runs through Gemini API, no lock needed)
- `keyboard-backend` — whisper call (Gemini correction is outside the lock)
- `marker-pipeline-backend` — `marker_single` subprocess
- `flashcard-backend` — currently no GPU work (regenerate-examples is Gemini-only)

## Adding a new GPU consumer

When you build a new container that loads a model or hits an inference server, route it through here. Checklist:

- **Copy `gpu_lock.py`** from any existing consumer (e.g. `cp xyt/gpu_lock.py new-tool/backend/`). All four copies are identical and stay that way — if you change the helper, update everywhere or extract it to a shared base image.
- **Make sure the consumer is on `my_network`** in its `docker-compose.yml` — the lock client resolves `http://gpu-broker:8000` via container DNS.
- **Add the HTTP client dependency** to `requirements.txt`: sync code needs `requests`; async (`async def`) handlers need `httpx`.
- **Wrap every GPU-touching call** with `gpu_lock(container, workload)` (sync) or `gpu_lock_async(container, workload)` (async). The `workload` string shows up on the dashboard — make it specific (`whisper:{job_id}`, `marker:{book_id}`) so you can tell what's running at a glance.
- **If you bring back a local LLM server** (e.g. re-enable ollama for a privacy-sensitive consumer), set `keep_alive` explicitly per request — `0` for one-shot, `"30s"` mid-batch with a final `0` cleanup — so the model unloads before the next lock holder acquires.
- **For Dockerfiles using `COPY <specific files>`** (rather than `COPY . .`), remember to add `gpu_lock.py` to the COPY line. Easy to miss — keyboard's Dockerfile hit this exact bug during the initial rollout.
- **No host setup** — the broker owns its own state, no `/var/run/...` bind-mount needed.

### What counts as a "GPU-touching call"

- Loading a model directly in-process (e.g. `whisper.load_model(...)`, `torch.load(...)`)
- Starting a subprocess that loads a model (e.g. `marker_single`, xyt's `multiprocessing.spawn` whisper)
- HTTP call to a containerized model server on this host (e.g. `keyboard-whisper:8000`, or `ollama:11434` if you ever revive it)

Cloud LLM calls (Gemini, OpenAI, etc.) do **not** count — they don't touch your GPU. CPU-only inference doesn't either.

## Run

```sh
docker compose up -d
```

Dashboard at `http://gpu-broker:8000/` from any container on `my_network`. From the host, route through `cloudflared` or `docker exec gpu-broker curl localhost:8000/api/state`.

## Failure modes

- **Broker down**: consumers print `[gpu-lock] broker unreachable, proceeding without lock` and proceed. GPU contention is back to its un-coordinated baseline until the broker comes back up. No deadlocks.
- **Client crashes mid-job**: the lease leaks (broker still thinks they're holding). Click "Force release" on the dashboard.
- **Broker restart with queue**: queue is wiped. In-flight `POST /acquire` calls fail with connection error; clients fall back to no-op as above.

## Known limits (acceptable for current scale)

- No priority — strict FIFO. If keyboard push-to-talk is queued behind a 30-min whisper job, it waits 30 min. Could add priority levels later.
- No persistent history — last 30 jobs only, lost on restart.
- No auth on `/api/holder` — anyone on `my_network` can force-release. Fine for single-user homelab.
