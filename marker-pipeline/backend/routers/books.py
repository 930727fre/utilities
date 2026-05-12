import io
import os
import re
import secrets
import shutil
import asyncio
import zipfile
from pathlib import Path
from datetime import datetime, timezone
import aiofiles
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse

import storage
from conversion import ACCEPTED_EXTENSIONS, convert_file, extract_meta

router = APIRouter(prefix="/api/books", tags=["books"])

DATA_DIR = storage.DATA_DIR

_CONTENT_TYPES = {
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
}


def _book_dir(book_id: str) -> str:
    return os.path.join(DATA_DIR, book_id)


def _source_path(book_id: str, ext: str) -> str:
    return os.path.join(_book_dir(book_id), f"{book_id}{ext}")


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/<>:"|?*\x00-\x1f]', "_", name).strip().strip(".")
    return name or "book"


@router.post("")
async def upload_book(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file type {ext!r}. Accepted: {sorted(ACCEPTED_EXTENSIONS)}",
        )

    book_id = secrets.token_hex(8)
    book_dir = _book_dir(book_id)
    os.makedirs(book_dir, exist_ok=True)
    src_path = _source_path(book_id, ext)

    async with aiofiles.open(src_path, "wb") as f:
        await f.write(await file.read())

    meta = extract_meta(src_path)
    title = meta["title"] or Path(filename).stem
    author = meta["author"]

    storage.add_book({
        "id": book_id,
        "title": title,
        "author": author,
        "status": "PARSING",
        "source_format": ext,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    background_tasks.add_task(_parse_book, book_id, src_path, book_dir)
    return {"book_id": book_id, "status": "PARSING"}


async def _parse_book(book_id: str, src_path: str, book_dir: str):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, convert_file, book_id, src_path, book_dir)
        storage.update_book(book_id, status="READY")
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


@router.get("/{book_id}/zip")
def download_zip(book_id: str):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if book.get("status") != "READY":
        raise HTTPException(409, f"Book is {book.get('status')}; not ready to download")

    book_dir = _book_dir(book_id)
    if not os.path.isdir(book_dir):
        raise HTTPException(404, "Book content not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(book_dir):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.relpath(full, book_dir)
                zf.write(full, arcname=arc)
    buf.seek(0)

    download_name = _safe_filename(book.get("title", "") or book_id) + ".zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@router.get("/{book_id}/source")
def get_source_file(book_id: str):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    ext = book.get("source_format", ".epub")
    path = _source_path(book_id, ext)
    if not os.path.exists(path):
        raise HTTPException(404, "Source file not found")
    return FileResponse(path, media_type=_CONTENT_TYPES.get(ext, "application/octet-stream"))


@router.delete("/{book_id}")
def delete_book(book_id: str):
    if not storage.get_book(book_id):
        raise HTTPException(404, "Book not found")
    storage.remove_book(book_id)
    book_dir = _book_dir(book_id)
    if os.path.exists(book_dir):
        shutil.rmtree(book_dir)
    return {"deleted": book_id}
