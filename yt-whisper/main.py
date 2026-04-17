import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from storage import read_jobs, get_job, upsert_job, ensure_jobs_file
from tasks import app as celery_app, process_video

DOWNLOADS_DIR = Path("/app/downloads")
STATIC_DIR = Path("/app/static")

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

ensure_jobs_file()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job(job_id: str, url: str) -> dict:
    return {
        "job_id": job_id,
        "url": url,
        "title": url,
        "status": "PENDING",
        "progress": {},
        "files": {"mp4": None, "srt": None},
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


# ── Pages ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


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


@app.post("/api/jobs", status_code=201)
async def submit_job(req: SubmitRequest):
    job_id = str(uuid.uuid4())
    job = _new_job(job_id, req.url)
    upsert_job(job)
    task = process_video.apply_async(args=[job_id, req.url], task_id=job_id)
    job["task_id"] = task.id
    upsert_job(job)
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
    process_video.apply_async(args=[job_id, job["url"]], task_id=job_id + "-retry")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    if job["status"] in ("DOWNLOADING", "TRANSCRIBING"):
        celery_app.control.revoke(job_id, terminate=True)

    for key in ("mp4", "srt"):
        path = job["files"].get(key)
        if path:
            full = Path("/app") / path
            full.unlink(missing_ok=True)

    job["status"] = "DELETED"
    job["updated_at"] = _now()
    upsert_job(job)
    return {"ok": True}


# ── Streaming ──────────────────────────────────────────────────────────────

@app.get("/api/stream/{job_id}/video")
async def stream_video(job_id: str, request: Request):
    job = get_job(job_id)
    if not job or not job["files"].get("mp4"):
        raise HTTPException(status_code=404, detail="Not found")

    path = Path("/app") / job["files"]["mp4"]
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

    path = Path("/app") / job["files"]["srt"]
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
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0f0f0f;color:#e5e5e5;font-family:sans-serif;display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:2rem 1rem}}
  h1{{font-size:1.1rem;margin-bottom:1rem;color:#d1d5db;max-width:900px;width:100%;text-align:center}}
  video{{width:100%;max-width:900px;border-radius:8px;background:#000}}
</style>
</head>
<body>
<h1>{title}</h1>
<video id="vid" controls autoplay>
  <source src="/api/stream/{job_id}/video" type="video/mp4">
  <track kind="subtitles" src="/api/stream/{job_id}/subtitle" default>
</video>
<script>
  document.getElementById('vid').play().catch(()=>{{}});
</script>
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
