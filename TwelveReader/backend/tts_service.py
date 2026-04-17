"""
TTS service — Kokoro-82M, sequential generation, sliding window cache.

Cache layout: /data/cache/{book_id}/{paragraph_id}.wav
Sliding window: [N-1][N playing][N+1 prefetch][N+2 prefetch] — max 4 files per book.
Eviction of N-2 is handled by the frontend on each paragraph advance.
"""

import asyncio
import os
import shutil
from typing import Optional

import numpy as np
import soundfile as sf
import torch

from database import DATA_DIR

MAX_RETRIES = 3


def _cache_path(book_id: str, paragraph_id: str) -> str:
    return os.path.join(DATA_DIR, "cache", book_id, f"{paragraph_id}.wav")


def _audio_url(book_id: str, paragraph_id: str) -> str:
    return f"/cache/{book_id}/{paragraph_id}.wav"


class TTSService:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._pipeline = None
        self._sample_rate: Optional[int] = None

    def _load_model(self):
        if self._pipeline is not None:
            return
        from kokoro import KPipeline
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[TTS] Loading Kokoro on {device}")
        self._pipeline = KPipeline(lang_code="a")   # American English
        self._sample_rate = 24000

    async def _generate_wav(self, text: str, out_path: str) -> bool:
        """Run Kokoro inference in a thread, write WAV. Returns True on success."""
        loop = asyncio.get_event_loop()

        def _run():
            self._load_model()
            generator = self._pipeline(text, voice="af_heart", speed=1.0)
            chunks = []
            for _, _, audio in generator:
                if audio is not None:
                    chunks.append(audio)
            if not chunks:
                return False
            audio_np = np.concatenate(chunks)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            sf.write(out_path, audio_np, self._sample_rate)
            return True

        async with self._lock:
            return await loop.run_in_executor(None, _run)

    async def get_or_generate(
        self, book_id: str, paragraph_id: str, text: str
    ) -> tuple[str, bool]:
        """
        Return (audio_url, cached).
        Generates if not cached. Retries up to MAX_RETRIES times.
        Falls back to tts_failed.wav URL after all retries exhausted.
        """
        path = _cache_path(book_id, paragraph_id)
        if os.path.exists(path):
            return _audio_url(book_id, paragraph_id), True

        for attempt in range(MAX_RETRIES):
            try:
                ok = await self._generate_wav(text, path)
                if ok and os.path.exists(path):
                    return _audio_url(book_id, paragraph_id), False
            except Exception as exc:
                print(f"[TTS] attempt {attempt + 1} failed: {exc}")

        # All retries failed — return fallback
        return "/audio/tts_failed.wav", False

    async def clear_cache(self, book_id: str):
        cache_dir = os.path.join(DATA_DIR, "cache", book_id)
        if os.path.exists(cache_dir):
            await asyncio.get_event_loop().run_in_executor(
                None, shutil.rmtree, cache_dir
            )


tts_service = TTSService()
