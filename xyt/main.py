import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse
from pydantic import BaseModel

from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from tasks import executor, process_video

DOWNLOADS_DIR = Path("/app/data/downloads")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_jobs_file()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    # Any job mid-flight (or queued) at startup is orphaned from a prior crash.
    # Mark FAILED so the UI surfaces ! / ↻ instead of an eternal ○.
    jobs = read_jobs()
    changed = False
    for job in jobs:
        if job["status"] in ("DOWNLOADING", "TRANSCRIBING", "PENDING"):
            job["status"] = "FAILED"
            job["error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned {job['job_id']} -> FAILED", flush=True)
    if changed:
        write_jobs(jobs)

    try:
        yield
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


def _new_job(job_id: str, url: str, transcribe: bool) -> dict:
    return {
        "job_id": job_id,
        "url": url,
        "title": url,
        "status": "PENDING",
        "progress": {},
        "files": {"mp4": None, "srt": None},
        "transcribe": transcribe,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


# ── Pages ──────────────────────────────────────────────────────────────────

@app.get("/player/{job_id}", response_class=HTMLResponse)
async def player(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "SUCCESS":
        return HTMLResponse(
            content=_player_not_ready_html(job["title"]),
            status_code=200,
        )
    return HTMLResponse(content=_player_html(job_id, job["title"]))


# ── API ────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    url: str
    transcribe: bool = True


@app.post("/api/jobs", status_code=201)
async def submit_job(req: SubmitRequest):
    job_id = str(uuid.uuid4())
    job = _new_job(job_id, req.url, req.transcribe)
    upsert_job(job)
    executor.submit(process_video, job_id, req.url, req.transcribe)
    return {"job_id": job_id, "status": "PENDING"}


@app.get("/api/jobs")
async def list_jobs():
    return [j for j in read_jobs() if j["status"] != "DELETED"]


@app.get("/api/jobs/{job_id}")
async def get_job_api(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        raise HTTPException(status_code=404, detail="Not found")
    return job


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] != "FAILED":
        raise HTTPException(status_code=400, detail="Job is not in FAILED state")
    job["status"] = "PENDING"
    job["error"] = None
    job["progress"] = {}
    job["updated_at"] = _now()
    upsert_job(job)
    executor.submit(process_video, job_id, job["url"], job.get("transcribe", True))
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    for key in ("mp4", "srt"):
        filename = job["files"].get(key)
        if filename:
            (DOWNLOADS_DIR / filename).unlink(missing_ok=True)

    job["status"] = "DELETED"
    job["updated_at"] = _now()
    upsert_job(job)
    return {"ok": True}


# ── Downloads ─────────────────────────────────────────────────────────────

@app.get("/api/download/{job_id}/mp4")
async def download_mp4(job_id: str):
    job = get_job(job_id)
    if not job or not job["files"].get("mp4"):
        raise HTTPException(status_code=404, detail="Not found")
    path = DOWNLOADS_DIR / job["files"]["mp4"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(path, filename=f"{job['title']}.mp4", media_type="video/mp4")


@app.get("/api/download/{job_id}/srt")
async def download_srt(job_id: str):
    job = get_job(job_id)
    if not job or not job["files"].get("srt"):
        raise HTTPException(status_code=404, detail="Not found")
    path = DOWNLOADS_DIR / job["files"]["srt"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(path, filename=f"{job['title']}.srt", media_type="text/plain")


# ── Streaming ──────────────────────────────────────────────────────────────

@app.get("/api/stream/{job_id}/video")
async def stream_video(job_id: str, request: Request):
    job = get_job(job_id)
    if not job or not job["files"].get("mp4"):
        raise HTTPException(status_code=404, detail="Not found")

    path = DOWNLOADS_DIR / job["files"]["mp4"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")

    file_size = path.stat().st_size
    start, end = 0, file_size - 1

    range = request.headers.get("range")
    if range:
        range_val = range.replace("bytes=", "")
        parts = range_val.split("-")
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else file_size - 1

    chunk_size = end - start + 1

    def iter_file():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                data = f.read(min(65536, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
    }
    status_code = 206 if range else 200
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type="video/mp4")


@app.get("/api/stream/{job_id}/subtitle")
async def stream_subtitle(job_id: str):
    job = get_job(job_id)
    if not job or not job["files"].get("srt"):
        raise HTTPException(status_code=404, detail="Not found")

    path = DOWNLOADS_DIR / job["files"]["srt"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")

    srt = path.read_text(encoding="utf-8")
    vtt = "WEBVTT\n\n" + srt.replace(",", ".", 1)
    # replace all timestamp commas (e.g. 00:00:01,000 → 00:00:01.000)
    import re
    vtt = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt)

    return Response(content=vtt, media_type="text/vtt")


# ── Player HTML helpers ────────────────────────────────────────────────────

def _player_html(job_id: str, title: str) -> str:
    media = f'<video id="vid" controls autoplay style="width:100%;max-width:900px;border-radius:8px;background:#000"><source src="/api/stream/{job_id}/video" type="video/mp4"><track kind="subtitles" src="/api/stream/{job_id}/subtitle" default></video>'
    transcript_html = '<script>document.getElementById(\'vid\').play().catch(()=>{});</script>'
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0f0f0f;color:#e5e5e5;font-family:sans-serif;display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:2rem 1rem;padding-bottom:4rem}}
  h1{{font-size:1.1rem;margin-bottom:1rem;color:#d1d5db;max-width:900px;width:100%;text-align:center}}
</style>
</head>
<body>
<h1>{title}</h1>
{media}
{transcript_html}
</body>
</html>"""


def _player_not_ready_html(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head><meta charset="UTF-8"><title>尚未就緒</title>
<style>body{{background:#0f0f0f;color:#e5e5e5;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh}}</style>
</head>
<body><p>影片尚未就緒：{title}</p></body>
</html>"""
