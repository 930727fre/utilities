import asyncio
import os
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse
from pydantic import BaseModel

from annotate import annotate_executor, annotate_job
from storage import ensure_jobs_file, get_job, read_jobs, upsert_job, write_jobs
from tasks import SCAN_DIR, enumerate_playlist, executor, process_video, scan_inbox, transcribe_file

DOWNLOADS_DIR = Path("/app/data/downloads")
INBOX_SCAN_INTERVAL = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_jobs_file()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    Path(SCAN_DIR).mkdir(parents=True, exist_ok=True)

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
        elif job["status"] == "ANNOTATING":
            # Annotation is optional; if it crashed mid-way, flip back to SUCCESS.
            # The .srt may be the original (unwritten) or partly written — user
            # can rerun by deleting + re-transcribing.
            job["status"] = "SUCCESS"
            job["annotation_error"] = "Interrupted by restart"
            job["updated_at"] = _now()
            changed = True
            print(f"[startup] orphaned annotation {job['job_id']} -> SUCCESS", flush=True)
    if changed:
        write_jobs(jobs)

    scan_task = asyncio.create_task(_inbox_scan_loop())
    try:
        yield
    finally:
        scan_task.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        annotate_executor.shutdown(wait=False, cancel_futures=True)


async def _inbox_scan_loop():
    while True:
        try:
            scan_inbox()
        except Exception as exc:
            print(f"[scan_inbox] {exc}", flush=True)
        await asyncio.sleep(INBOX_SCAN_INTERVAL)


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


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
    has_annotated = bool(job["files"].get("srt_annotated"))
    return HTMLResponse(content=_player_html(job_id, job["title"], audio_only=is_audio, has_annotated=has_annotated))


# ── API ────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    url: str


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def _is_playlist_url(url: str) -> bool:
    """True only for canonical playlist URLs (`/playlist?list=…`).

    `watch?v=…&list=…` is intentionally NOT treated as a playlist — the user
    typically means the single video that happens to be inside one.
    """
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.netloc or "").lower().split(":", 1)[0]
    return host in _YT_HOSTS and u.path == "/playlist" and "list" in parse_qs(u.query)


@app.post("/api/jobs", status_code=201)
async def submit_job(req: SubmitRequest):
    if _is_playlist_url(req.url):
        try:
            entries = await asyncio.to_thread(enumerate_playlist, req.url)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Playlist enumeration failed: {e}")
        if not entries:
            raise HTTPException(status_code=400, detail="Playlist is empty or all entries unavailable")
        job_ids = []
        for entry in entries:
            job_id = str(uuid.uuid4())
            job = _new_job(job_id, entry["url"])
            if entry["title"]:
                job["title"] = entry["title"]
            upsert_job(job)
            executor.submit(process_video, job_id, entry["url"])
            job_ids.append(job_id)
        return {"playlist": True, "count": len(job_ids), "job_ids": job_ids}

    job_id = str(uuid.uuid4())
    job = _new_job(job_id, req.url)
    upsert_job(job)
    executor.submit(process_video, job_id, req.url)
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
    if job.get("source_file"):
        executor.submit(transcribe_file, job_id, os.path.join(SCAN_DIR, job["source_file"]))
    else:
        executor.submit(process_video, job_id, job["url"])
    return {"ok": True}


@app.post("/api/jobs/{job_id}/annotate")
async def annotate_job_api(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        raise HTTPException(status_code=404, detail="Not found")
    if job["status"] != "SUCCESS":
        raise HTTPException(status_code=400, detail="Job is not in SUCCESS state")
    if job.get("annotated"):
        raise HTTPException(status_code=400, detail="Job is already annotated")
    if not (job.get("files") or {}).get("srt"):
        raise HTTPException(status_code=400, detail="Job has no SRT to annotate")
    job["status"] = "ANNOTATING"
    job["annotation_error"] = None
    job["updated_at"] = _now()
    upsert_job(job)
    annotate_executor.submit(annotate_job, job_id)
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


@app.get("/api/download/{job_id}/zip")
async def download_zip(job_id: str, background_tasks: BackgroundTasks):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Not found")

    title = job["title"]
    members: list[tuple[Path, str]] = []
    # mp4 / srt / srt_annotated live under DOWNLOADS_DIR; mp3 (inbox jobs)
    # is an absolute path stored at submission time.
    for kind, ext in (("mp4", "mp4"), ("mp3", "mp3"), ("srt", "srt"),
                     ("srt_annotated", "annotated.srt")):
        ref = job["files"].get(kind)
        if not ref:
            continue
        path = Path(ref) if kind == "mp3" else DOWNLOADS_DIR / ref
        if path.exists():
            members.append((path, f"{title}.{ext}"))

    if not members:
        raise HTTPException(status_code=404, detail="No files to download")

    # ZIP_STORED — media is already compressed; deflate would waste CPU for ~0% gain.
    tmp_path = tempfile.mktemp(suffix=".zip", dir=str(DOWNLOADS_DIR))
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        for src, arcname in members:
            zf.write(src, arcname=arcname)

    background_tasks.add_task(lambda p=tmp_path: os.unlink(p) if os.path.exists(p) else None)
    return FileResponse(tmp_path, filename=f"{title}.zip", media_type="application/zip")


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


def _serve_vtt(job_id: str, file_key: str) -> Response:
    import re
    job = get_job(job_id)
    if not job or not job["files"].get(file_key):
        raise HTTPException(status_code=404, detail="Not found")
    path = DOWNLOADS_DIR / job["files"][file_key]
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    srt = path.read_text(encoding="utf-8")
    # SRT timestamp commas → VTT periods (e.g. 00:00:01,000 → 00:00:01.000)
    vtt = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", srt)
    return Response(content=vtt, media_type="text/vtt")


@app.get("/api/stream/{job_id}/subtitle")
async def stream_subtitle(job_id: str):
    return _serve_vtt(job_id, "srt")


@app.get("/api/stream/{job_id}/subtitle_annotated")
async def stream_subtitle_annotated(job_id: str):
    return _serve_vtt(job_id, "srt_annotated")


# ── Player HTML helpers ────────────────────────────────────────────────────

def _player_html(job_id: str, title: str, audio_only: bool = False, has_annotated: bool = False) -> str:
    # Resume-position script — same for audio and video (both use id="vid").
    # Restores on loadedmetadata, throttle-saves every 1 s, clears within 10 s
    # of the end so re-watching doesn't dump the user back at the credits.
    resume_script = f"""
<script>
(() => {{
  const KEY = 'transcribe:resume:{job_id}';
  const vid = document.getElementById('vid');
  vid.addEventListener('loadedmetadata', () => {{
    const t = parseFloat(localStorage.getItem(KEY) || '');
    if (Number.isFinite(t) && t > 5) vid.currentTime = t;
  }}, {{ once: true }});
  function save() {{
    const t = vid.currentTime;
    if (!Number.isFinite(t) || t < 5) return;
    const dur = vid.duration;
    if (Number.isFinite(dur) && dur > 0 && dur - t < 10) {{
      localStorage.removeItem(KEY);
      return;
    }}
    localStorage.setItem(KEY, String(t));
  }}
  let last = 0;
  vid.addEventListener('timeupdate', () => {{
    const now = Date.now();
    if (now - last > 1000) {{ last = now; save(); }}
  }});
  vid.addEventListener('pause', save);
  vid.addEventListener('ended', () => localStorage.removeItem(KEY));
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'hidden') save();
  }});
}})();
</script>"""

    if audio_only:
        media = f'<audio id="vid" controls autoplay style="width:100%;max-width:900px"><source src="/api/stream/{job_id}/audio" type="audio/mpeg"></audio>'
        toggle_html = (
            f'<div style="width:100%;max-width:900px;margin-top:1rem;display:flex;gap:8px;font-size:0.85rem">'
            f'<button id="srt-orig" style="background:none;border:1px solid #3a3a3c;color:#aeaeb2;border-radius:6px;padding:4px 10px;cursor:pointer">Original</button>'
            f'<button id="srt-anno" style="background:#1d2535;border:1px solid #c79968;color:#e8e3d9;border-radius:6px;padding:4px 10px;cursor:pointer">Annotated</button>'
            f'</div>'
        ) if has_annotated else ''
        initial_variant = "subtitle_annotated" if has_annotated else "subtitle"
        transcript_html = toggle_html + f"""
<div id="transcript" style="width:100%;max-width:900px;margin-top:1rem;display:flex;flex-direction:column;gap:4px"></div>
<script>
  const audio = document.getElementById('vid');
  audio.play().catch(()=>{{}});

  let cues = [];
  let currentVariant = '{initial_variant}';

  async function loadTranscript() {{
    cues = [];
    const res = await fetch('/api/stream/{job_id}/' + currentVariant);
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

  const origBtn = document.getElementById('srt-orig');
  const annoBtn = document.getElementById('srt-anno');
  function styleButtons() {{
    const active = {{ background: '#1d2535', borderColor: '#c79968', color: '#e8e3d9' }};
    const idle = {{ background: 'none', borderColor: '#3a3a3c', color: '#aeaeb2' }};
    const o = currentVariant === 'subtitle' ? active : idle;
    const a = currentVariant === 'subtitle_annotated' ? active : idle;
    if (origBtn) Object.assign(origBtn.style, o);
    if (annoBtn) Object.assign(annoBtn.style, a);
  }}
  if (origBtn) origBtn.onclick = () => {{ currentVariant = 'subtitle'; styleButtons(); loadTranscript(); }};
  if (annoBtn) annoBtn.onclick = () => {{ currentVariant = 'subtitle_annotated'; styleButtons(); loadTranscript(); }};

  loadTranscript();
</script>"""
    else:
        if has_annotated:
            tracks = (
                f'<track kind="subtitles" srclang="en" label="Original" src="/api/stream/{job_id}/subtitle">'
                f'<track kind="subtitles" srclang="zh" label="Annotated" src="/api/stream/{job_id}/subtitle_annotated" default>'
            )
        else:
            tracks = f'<track kind="subtitles" src="/api/stream/{job_id}/subtitle" default>'
        media = f'<video id="vid" controls autoplay style="width:100%;max-width:900px;border-radius:8px;background:#000"><source src="/api/stream/{job_id}/video" type="video/mp4">{tracks}</video>'
        transcript_html = ''
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
{resume_script}
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
