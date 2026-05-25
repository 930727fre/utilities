import functools
import json
import multiprocessing
import os
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pykakasi
import yt_dlp

from gemini_client import generate_json
from gpu_lock import gpu_lock
from storage import get_job, upsert_job

DOWNLOADS_DIR = "/app/data/downloads"

DOWNLOAD_TIMEOUT = 60 * 60        # 1 hour
TRANSCRIBE_TIMEOUT = 4 * 60 * 60  # 4 hours
ENRICH_TIMEOUT = 2 * 60 * 60      # 2 hours

# Translate this many cues per Gemini request. ~30 amortizes round-trip latency
# 30x while keeping batches small enough that an alignment mismatch only costs
# a fallback for that batch (not 100+ cues).
BATCH_SIZE = 30

_kks = pykakasi.kakasi()

# Single GPU → serialize work to one job at a time.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe-worker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed(since_iso: str) -> float:
    start = datetime.fromisoformat(since_iso)
    return (datetime.now(timezone.utc) - start).total_seconds()


def _catch_unhandled(fn):
    @functools.wraps(fn)
    def wrapped(job_id, *args, **kwargs):
        try:
            return fn(job_id, *args, **kwargs)
        except Exception as exc:
            traceback.print_exc()
            _fail(job_id, f"Unhandled error: {exc}")
    return wrapped


@_catch_unhandled
def process_video(job_id: str, url: str, transcribe: bool = True):
    job = get_job(job_id)
    if not job or job["status"] in ("DELETED", "SUCCESS", "DOWNLOADING", "TRANSCRIBING", "ENRICHING"):
        return

    base_path = os.path.join(DOWNLOADS_DIR, job_id)

    job["status"] = "DOWNLOADING"
    job["updated_at"] = _now()
    upsert_job(job)

    download_started = _now()

    def progress_hook(d):
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            raise Exception("Job cancelled")
        if _elapsed(download_started) > DOWNLOAD_TIMEOUT:
            raise Exception("Download timed out (1 hour limit)")

    ydl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]",
        "merge_output_format": "mp4",
        "outtmpl": base_path + ".%(ext)s",
        "progress_hooks": [progress_hook],
        "quiet": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", job_id)
    except Exception as e:
        _fail(job_id, str(e))
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    job["title"] = title
    job["files"]["mp4"] = f"{job_id}.mp4"

    if not transcribe:
        job["status"] = "SUCCESS"
        job["updated_at"] = _now()
        upsert_job(job)
        return

    job["status"] = "TRANSCRIBING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_transcription(job_id, base_path + ".mp4")


def _run_transcription(job_id: str, audio_path: str):
    result_file = tempfile.mktemp(suffix=".json")
    ctx = multiprocessing.get_context("spawn")
    transcribe_started = _now()

    with gpu_lock("xyt-app", f"whisper:{job_id}"):
        proc = ctx.Process(target=_transcribe_worker, args=(audio_path, result_file))
        proc.start()

        while True:
            proc.join(timeout=2)
            if not proc.is_alive():
                break

            if _elapsed(transcribe_started) > TRANSCRIBE_TIMEOUT:
                proc.terminate()
                proc.join()
                if os.path.exists(result_file):
                    os.unlink(result_file)
                _fail(job_id, "Transcription timed out (4 hour limit)")
                return

            current = get_job(job_id)
            if not current or current["status"] == "DELETED":
                proc.terminate()
                proc.join()
                if os.path.exists(result_file):
                    os.unlink(result_file)
                return

    if not os.path.exists(result_file):
        _fail(job_id, "Transcription process exited unexpectedly")
        return

    with open(result_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    os.unlink(result_file)

    if "error" in payload:
        _fail(job_id, payload["error"])
        return

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    srt_path = os.path.join(DOWNLOADS_DIR, job_id + ".srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(payload["segments"], 1):
            f.write(f"{i}\n")
            f.write(f"{_fmt_time(seg['start'])} --> {_fmt_time(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")

    job["files"]["srt"] = f"{job_id}.srt"
    job["status"] = "ENRICHING"
    job["updated_at"] = _now()
    upsert_job(job)

    _run_enrichment(job_id, srt_path)


def _run_enrichment(job_id: str, srt_path: str):
    cues = _parse_srt(srt_path)
    started = _now()

    def alive() -> bool:
        if _elapsed(started) > ENRICH_TIMEOUT:
            _fail(job_id, "Enrichment timed out (2 hour limit)")
            return False
        current = get_job(job_id)
        if not current or current["status"] == "DELETED":
            return False
        return True

    # Translate in batches against Gemini. Cloud call, no GPU lock needed.
    texts = [text for _, _, text in cues]
    translations = [""] * len(texts)
    for batch_start in range(0, len(texts), BATCH_SIZE):
        if not alive():
            return
        batch_end = min(batch_start + BATCH_SIZE, len(texts))
        batch = texts[batch_start:batch_end]
        try:
            batch_translations = _translate_batch(batch)
        except Exception as e:
            _fail(job_id, f"Translation failed at batch {batch_start}-{batch_end}: {e}")
            return
        for i, t in enumerate(batch_translations):
            translations[batch_start + i] = t
        print(f"[xyt] enrichment {batch_end}/{len(texts)}", flush=True)

    # Romanize locally (pykakasi, CPU, deterministic) and stack.
    combined = []
    for (idx, time_line, text), zh in zip(cues, translations):
        romaji = _romanize(text)
        stacked = "\n".join(s for s in (text, romaji, zh) if s)
        combined.append((idx, time_line, stacked))

    _write_srt(srt_path, combined)

    job = get_job(job_id)
    if not job or job["status"] == "DELETED":
        return
    job["status"] = "SUCCESS"
    job["updated_at"] = _now()
    upsert_job(job)


_TRANSLATION_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
}


def _translate_batch(texts: list[str]) -> list[str]:
    """Translate N Japanese lines → N zh-Hant lines in one Gemini call.

    Falls back to per-line translation if the batch returns the wrong count
    (model dropped or merged lines despite the structured-output schema).
    """
    if not texts:
        return []

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "Translate the following numbered Japanese subtitle lines to Traditional Chinese.\n"
        "Return a JSON array of strings, one translation per input line, in the same order.\n"
        "Translate only. No romanization, no Japanese, no commentary.\n"
        "If a line is empty or untranslatable, return an empty string for that position.\n\n"
        f"{numbered}"
    )
    result = generate_json(prompt, _TRANSLATION_SCHEMA)
    if isinstance(result, list) and len(result) == len(texts):
        return [str(t).strip() for t in result]

    # Length mismatch — fall back to per-line so we don't drop the batch.
    print(f"[xyt] batch returned {len(result) if isinstance(result, list) else '?'} "
          f"items for {len(texts)} inputs, falling back to per-line", flush=True)
    return [_translate_one(t) for t in texts]


def _translate_one(text: str) -> str:
    if not text.strip():
        return ""
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    prompt = (
        "Translate the following Japanese subtitle line to Traditional Chinese.\n"
        "Return JSON with a single `text` field. Translate only. No romanization, no Japanese, no commentary.\n\n"
        f"Japanese: {text}"
    )
    result = generate_json(prompt, schema)
    return str(result.get("text", "")).strip()


def _parse_srt(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    out = []
    for block in content.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        out.append((lines[0], lines[1], "\n".join(lines[2:])))
    return out


def _write_srt(path: str, cues: list):
    with open(path, "w", encoding="utf-8") as f:
        for idx, time_line, text in cues:
            f.write(f"{idx}\n{time_line}\n{text}\n\n")


def _romanize(text: str) -> str:
    if not text.strip():
        return ""
    items = _kks.convert(text)
    return " ".join(item["hepburn"] for item in items).strip()


def _transcribe_worker(mp4_path: str, result_file: str):
    import json
    import torch
    import whisper
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[transcribe] device={device}", flush=True)
    try:
        model = whisper.load_model("medium", device=device)
        result = model.transcribe(mp4_path, beam_size=5, language=None, verbose=False)
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"error": str(e)}, f)


def _fail(job_id: str, error: str):
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "FAILED"
    job["error"] = error
    job["updated_at"] = _now()
    upsert_job(job)


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
