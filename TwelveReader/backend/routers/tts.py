import os
from fastapi import APIRouter, HTTPException
from database import get_conn
from tts_service import tts_service, _cache_path

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.post("/{book_id}/{paragraph_id}")
async def generate_tts(book_id: str, paragraph_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT text FROM paragraphs WHERE book_id=? AND paragraph_id=?",
            (book_id, paragraph_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Paragraph not found")

    url, cached = await tts_service.get_or_generate(book_id, paragraph_id, row["text"])
    return {"url": url, "cached": cached}


@router.delete("/{book_id}/cache")
async def clear_cache(book_id: str):
    await tts_service.clear_cache(book_id)
    return {"cleared": book_id}


@router.delete("/{book_id}/{paragraph_id}")
async def evict_cached(book_id: str, paragraph_id: str):
    path = _cache_path(book_id, paragraph_id)
    if os.path.exists(path):
        os.remove(path)
    return {"evicted": paragraph_id}
