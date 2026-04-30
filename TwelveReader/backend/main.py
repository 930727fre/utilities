import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import storage
from routers import books, bookmarks, tts

DATA_DIR = storage.DATA_DIR
STATIC_DIR = os.environ.get("STATIC_DIR", "/data/static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "cache"), exist_ok=True)
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
app.include_router(bookmarks.router)
app.include_router(tts.router)

app.mount("/cache", StaticFiles(directory=os.path.join(DATA_DIR, "cache")), name="cache")
app.mount("/audio", StaticFiles(directory=STATIC_DIR), name="audio")


def _ensure_fallback_audio():
    out_path = os.path.join(STATIC_DIR, "tts_failed.wav")
    if os.path.exists(out_path):
        return
    try:
        import numpy as np
        import soundfile as sf
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
