"""GPU lock client — talks to the gpu-broker service.

`with gpu_lock("xyt-app", "whisper")` blocks until the broker grants the GPU,
then releases it on exit. `gpu_lock_async` is the same shape for asyncio code.

If the broker is unreachable, both fall back to "proceed without lock" so a
broker outage degrades to "no coordination," not "everything hangs."
"""
import contextlib
import os
from typing import Optional

BROKER_URL = os.getenv("GPU_BROKER_URL", "http://gpu-broker:8000")
# Connect timeout: how long to wait for the broker socket. Short — broker is on
# the same docker network and either responds in ms or is genuinely down.
# Read timeout: None — the broker holds our request open until it's our turn,
# which can be minutes for whisper-medium queues. Don't artificially time out.
_TIMEOUT = (5, None)


def _acquire_sync(container: str, workload: str, eta: Optional[float]) -> Optional[str]:
    import requests  # lazy: async-only callers (keyboard) don't need requests installed
    try:
        r = requests.post(
            f"{BROKER_URL}/acquire",
            json={"container": container, "workload": workload, "eta_seconds": eta},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:
        print(f"[gpu-lock] broker unreachable, proceeding without lock: {e}", flush=True)
        return None


def _release_sync(token: Optional[str]):
    if not token:
        return
    import requests
    try:
        requests.delete(f"{BROKER_URL}/lease/{token}", timeout=5)
    except Exception as e:
        print(f"[gpu-lock] release failed: {e}", flush=True)


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
