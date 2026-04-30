import os
import secrets
import shutil
import asyncio
import aiofiles
from datetime import datetime, timezone
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, Response as RawResponse

import storage
from epub_parser import convert_epub, load_md

router = APIRouter(prefix="/api/books", tags=["books"])

DATA_DIR = storage.DATA_DIR


def _book_dir(book_id: str) -> str:
    return os.path.join(DATA_DIR, book_id)


@router.post("")
async def upload_book(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.endswith(".epub"):
        raise HTTPException(400, "Only .epub files are supported")

    book_id = secrets.token_hex(8)
    book_dir = _book_dir(book_id)
    os.makedirs(book_dir, exist_ok=True)
    epub_path = os.path.join(book_dir, f"{book_id}.epub")

    async with aiofiles.open(epub_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    storage.add_book({
        "id": book_id,
        "title": file.filename,
        "author": "",
        "status": "PARSING",
        "bookmark_paragraph_index": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    background_tasks.add_task(_parse_book, book_id, epub_path, book_dir)
    return {"book_id": book_id, "status": "PARSING"}


async def _parse_book(book_id: str, epub_path: str, book_dir: str):
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, convert_epub, book_id, epub_path, book_dir
        )
        storage.update_book(
            book_id,
            title=result["meta"]["title"],
            author=result["meta"]["author"],
            status="READY",
        )
    except Exception as exc:
        print(f"[parse] FAILED book={book_id}: {exc}")
        storage.update_book(book_id, status="FAILED")


@router.get("")
def list_books():
    return storage.list_books()


@router.get("/{book_id}")
def get_book(book_id: str):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    return book


@router.get("/{book_id}/md")
def get_md(book_id: str):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    try:
        md = load_md(_book_dir(book_id), book_id)
    except FileNotFoundError:
        raise HTTPException(404, "MD file not found")
    return RawResponse(content=md, media_type="text/markdown; charset=utf-8")


@router.get("/{book_id}/assets/{path:path}")
def get_asset(book_id: str, path: str):
    book_dir = _book_dir(book_id)
    file_path = os.path.realpath(os.path.join(book_dir, path))
    if not file_path.startswith(os.path.realpath(book_dir)):
        raise HTTPException(403, "Access denied")
    if not os.path.exists(file_path):
        raise HTTPException(404, "Asset not found")
    return FileResponse(file_path)


@router.get("/{book_id}/epub")
def get_epub_file(book_id: str):
    epub_path = os.path.join(_book_dir(book_id), f"{book_id}.epub")
    if not os.path.exists(epub_path):
        raise HTTPException(404, "EPUB not found")
    return FileResponse(epub_path, media_type="application/epub+zip")


@router.delete("/{book_id}")
def delete_book(book_id: str):
    if not storage.get_book(book_id):
        raise HTTPException(404, "Book not found")
    storage.remove_book(book_id)
    for d in (_book_dir(book_id), os.path.join(DATA_DIR, "cache", book_id)):
        if os.path.exists(d):
            shutil.rmtree(d)
    return {"deleted": book_id}
