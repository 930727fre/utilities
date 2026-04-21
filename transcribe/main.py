import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from storage import read_jobs, get_job, upsert_job, ensure_jobs_file
from tasks import process_video, scan_inbox

DOWNLOADS_DIR = Path("/app/data/downloads")
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
    is_audio = bool(job["files"].get("mp3")) and not job["files"].get("mp4")
    return HTMLResponse(content=_player_html(job_id, job["title"], audio_only=is_audio))


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

    for key in ("mp4", "srt"):
        filename = job["files"].get(key)
        if filename:
            (DOWNLOADS_DIR / filename).unlink(missing_ok=True)
    if job["files"].get("mp3"):
        Path(job["files"]["mp3"]).unlink(missing_ok=True)

    job["status"] = "DELETED"
    job["updated_at"] = _now()
    upsert_job(job)
    return {"ok": True}


# ── Scan ───────────────────────────────────────────────────────────────────

@app.post("/api/scan")
async def scan():
    count = scan_inbox()
    return {"queued": count}


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


@app.get("/api/download/{job_id}/mp3")
async def download_mp3(job_id: str):
    job = get_job(job_id)
    if not job or not job["files"].get("mp3"):
        raise HTTPException(status_code=404, detail="Not found")
    path = Path(job["files"]["mp3"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(path, filename=f"{job['title']}.mp3", media_type="audio/mpeg")


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


@app.get("/api/stream/{job_id}/audio")
async def stream_audio(job_id: str, request: Request):
    job = get_job(job_id)
    if not job or not job["files"].get("mp3"):
        raise HTTPException(status_code=404, detail="Not found")

    path = Path(job["files"]["mp3"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")

    file_size = path.stat().st_size
    start, end = 0, file_size - 1

    range_header = request.headers.get("range")
    if range_header:
        range_val = range_header.replace("bytes=", "")
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
    status_code = 206 if range_header else 200
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type="audio/mpeg")


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

def _player_html(job_id: str, title: str, audio_only: bool = False) -> str:
    if audio_only:
        media = f'<audio id="vid" controls autoplay style="width:100%;max-width:900px"><source src="/api/stream/{job_id}/audio" type="audio/mpeg"></audio>'
        transcript_html = f"""
<div id="transcript" style="width:100%;max-width:900px;margin-top:1.5rem;display:flex;flex-direction:column;gap:4px"></div>
<script>
  const audio = document.getElementById('vid');
  audio.play().catch(()=>{{}});

  let cues = [];

  async function loadTranscript() {{
    const res = await fetch('/api/stream/{job_id}/subtitle');
    const vtt = await res.text();
    const lines = vtt.split('\\n');
    let i = 0;
    while (i < lines.length) {{
      const timeLine = lines[i].match(/(\d{{2}}:\d{{2}}:\d{{2}}\.\d{{3}}) --> (\d{{2}}:\d{{2}}:\d{{2}}\.\d{{3}})/);
      if (timeLine) {{
        const start = parseVttTime(timeLine[1]);
        const end = parseVttTime(timeLine[2]);
        let text = '';
        i++;
        while (i < lines.length && lines[i].trim() !== '') {{
          text += (text ? ' ' : '') + lines[i].trim();
          i++;
        }}
        cues.push({{ start, end, text }});
      }} else {{
        i++;
      }}
    }}
    render();
  }}

  function parseVttTime(t) {{
    const [h, m, s] = t.split(':');
    return parseInt(h) * 3600 + parseInt(m) * 60 + parseFloat(s);
  }}

  function render() {{
    const el = document.getElementById('transcript');
    el.innerHTML = cues.map((c, i) =>
      `<div data-i="${{i}}" onclick="seek(${{c.start}})" style="padding:6px 10px;border-radius:6px;cursor:pointer;font-size:0.9rem;line-height:1.5;color:#9ca3af;transition:background .15s">${{c.text}}</div>`
    ).join('');
  }}

  function seek(t) {{ audio.currentTime = t; audio.play(); }}

  audio.addEventListener('timeupdate', () => {{
    const t = audio.currentTime;
    cues.forEach((c, i) => {{
      const el = document.querySelector(`[data-i="${{i}}"]`);
      if (!el) return;
      const active = t >= c.start && t < c.end;
      el.style.background = active ? '#1d2535' : '';
      el.style.color = active ? '#f3f4f6' : '#9ca3af';
      if (active) el.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
    }});
  }});

  loadTranscript();
</script>"""
    else:
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
