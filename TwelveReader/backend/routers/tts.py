import os
from fastapi import APIRouter, HTTPException

import storage
from epub_parser import load_md, split_paragraphs, paragraph_id as make_paragraph_id
from tts_service import tts_service, _cache_path

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.delete("/{book_id}/cache")
async def clear_cache(book_id: str):
    await tts_service.clear_cache(book_id)
    return {"cleared": book_id}


@router.post("/{book_id}/{index}")
async def generate_tts(book_id: str, index: int):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    book_dir = os.path.join(storage.DATA_DIR, book_id)
    try:
        md = load_md(book_dir, book_id)
    except FileNotFoundError:
        raise HTTPException(404, "Book content not found")

    paragraphs = split_paragraphs(md)
    if index < 0 or index >= len(paragraphs):
        raise HTTPException(404, "Paragraph index out of range")

    text = paragraphs[index]
    pid = make_paragraph_id(book_id, index, text)
    url, cached = await tts_service.get_or_generate(book_id, pid, text)
    return {"url": url, "cached": cached}


@router.delete("/{book_id}/{index}")
async def evict_cached(book_id: str, index: int):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    book_dir = os.path.join(storage.DATA_DIR, book_id)
    try:
        md = load_md(book_dir, book_id)
    except FileNotFoundError:
        return {"evicted": index}

    paragraphs = split_paragraphs(md)
    if 0 <= index < len(paragraphs):
        pid = make_paragraph_id(book_id, index, paragraphs[index])
        path = _cache_path(book_id, pid)
        if os.path.exists(path):
            os.remove(path)
    return {"evicted": index}
