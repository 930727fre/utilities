import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import init_db, DATA_DIR, STATIC_DIR
from routers import books, paragraphs, bookmarks, tts


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _ensure_fallback_audio()
    yield


app = FastAPI(title="TwelveReader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(books.router)
app.include_router(paragraphs.router)
app.include_router(bookmarks.router)
app.include_router(tts.router)

# Serve generated audio files
cache_dir = os.path.join(DATA_DIR, "cache")
os.makedirs(cache_dir, exist_ok=True)
app.mount("/cache", StaticFiles(directory=cache_dir), name="cache")

# Serve fallback audio
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=STATIC_DIR), name="audio")


def _ensure_fallback_audio():
    """Generate tts_failed.wav using Kokoro on first startup if missing."""
    out_path = os.path.join(STATIC_DIR, "tts_failed.wav")
    if os.path.exists(out_path):
        return
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from kokoro import KPipeline

        pipeline = KPipeline(lang_code="a")
        chunks = []
        for _, _, audio in pipeline(
            "This paragraph failed to convert.", voice="af_heart", speed=1.0
        ):
            if audio is not None:
                chunks.append(audio)
        if chunks:
            audio_np = np.concatenate(chunks)
            sf.write(out_path, audio_np, 24000)
            print("[startup] tts_failed.wav generated")
    except Exception as exc:
        print(f"[startup] Could not generate tts_failed.wav: {exc}")


@app.get("/health")
def health():
    return {"ok": True}
