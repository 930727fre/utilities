"""GPU lock client — talks to the gpu-broker service.

`with gpu_lock("xyt-app", "whisper")` blocks until the broker grants the GPU,
then releases it on exit. `gpu_lock_async` is the same shape for asyncio code.

If the broker is unreachable, both fall back to "proceed without lock" so a
broker outage degrades to "no coordination," not "everything hangs."
"""
import contextlib
import os
import threading
from typing import Optional

BROKER_URL = os.getenv("GPU_BROKER_URL", "http://gpu-broker:8000")
# Connect timeout: how long to wait for the broker socket. Short — broker is on
# the same docker network and either responds in ms or is genuinely down.
# Read timeout: None — the broker holds our request open until it's our turn,
# which can be minutes for whisper-medium queues. Don't artificially time out.
_TIMEOUT = (5, None)

# Held tokens, so `release_all_held()` can run at shutdown and free leases the
# broker would otherwise consider stuck (broker has no TTL / heartbeat).
_active_tokens: set[str] = set()
_tokens_lock = threading.Lock()


def _acquire_sync(container: str, workload: str, eta: Optional[float]) -> Optional[str]:
    import requests  # lazy: async-only callers (keyboard) don't need requests installed
    try:
        r = requests.post(
            f"{BROKER_URL}/acquire",
            json={"container": container, "workload": workload, "eta_seconds": eta},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        token = r.json()["token"]
        with _tokens_lock:
            _active_tokens.add(token)
        return token
    except Exception as e:
        print(f"[gpu-lock] broker unreachable, proceeding without lock: {e}", flush=True)
        return None


def _release_sync(token: Optional[str]):
    if not token:
        return
    with _tokens_lock:
        _active_tokens.discard(token)
    import requests
    try:
        requests.delete(f"{BROKER_URL}/lease/{token}", timeout=5)
    except Exception as e:
        print(f"[gpu-lock] release failed: {e}", flush=True)


def release_all_held():
    """Best-effort: DELETE every lease this process is currently holding.

    Called at shutdown so SIGTERM from `docker compose down/up --build` doesn't
    leave a phantom holder in the broker's memory.
    """
    with _tokens_lock:
        tokens = list(_active_tokens)
        _active_tokens.clear()
    if not tokens:
        return
    import requests
    for t in tokens:
        try:
            requests.delete(f"{BROKER_URL}/lease/{t}", timeout=2)
            print(f"[gpu-lock] shutdown-released {t}", flush=True)
        except Exception as e:
            print(f"[gpu-lock] shutdown release failed for {t}: {e}", flush=True)


@contextlib.contextmanager
def gpu_lock(container: str, workload: str, eta_seconds: Optional[float] = None):
    """Block until broker grants GPU, hold it for the block, release on exit."""
    token = _acquire_sync(container, workload, eta_seconds)
    try:
        yield
    finally:
        _release_sync(token)


@contextlib.asynccontextmanager
async def gpu_lock_async(container: str, workload: str, eta_seconds: Optional[float] = None):
    """Async version using httpx so the event loop stays responsive while waiting."""
    import httpx  # local import — only async callers pay for the dependency

    token: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=None)) as client:
            r = await client.post(
                f"{BROKER_URL}/acquire",
                json={"container": container, "workload": workload, "eta_seconds": eta_seconds},
            )
            r.raise_for_status()
            token = r.json()["token"]
    except Exception as e:
        print(f"[gpu-lock] broker unreachable, proceeding without lock: {e}", flush=True)

    try:
        yield
    finally:
        if token:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.delete(f"{BROKER_URL}/lease/{token}")
            except Exception as e:
                print(f"[gpu-lock] release failed: {e}", flush=True)
