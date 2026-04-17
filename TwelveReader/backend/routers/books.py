import mimetypes
import os
import shutil
import uuid
import asyncio
import aiofiles
import ebooklib
from ebooklib import epub as epub_lib
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, Response as RawResponse

from database import get_conn, DATA_DIR
from epub_parser import parse_epub

router = APIRouter(prefix="/api/books", tags=["books"])


@router.post("")
async def upload_book(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.endswith(".epub"):
        raise HTTPException(400, "Only .epub files are supported")

    book_id = str(uuid.uuid4())
    book_dir = os.path.join(DATA_DIR, "books", book_id)
    os.makedirs(book_dir, exist_ok=True)
    epub_path = os.path.join(book_dir, "book.epub")

    async with aiofiles.open(epub_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO books (id, title, author, epub_path, status) VALUES (?, ?, ?, ?, ?)",
            (book_id, file.filename, "", epub_path, "PARSING"),
        )

    background_tasks.add_task(_parse_book, book_id, epub_path)
    return {"book_id": book_id, "status": "PARSING"}


async def _parse_book(book_id: str, epub_path: str):
    loop = asyncio.get_event_loop()
    try:
        meta, paragraphs = await loop.run_in_executor(
            None, parse_epub, epub_path, book_id
        )
        with get_conn() as conn:
            conn.execute(
                "UPDATE books SET title=?, author=?, status='READY' WHERE id=?",
                (meta["title"], meta["author"], book_id),
            )
            conn.executemany(
                """INSERT OR IGNORE INTO paragraphs
                   (book_id, paragraph_id, spine_href, para_index, tag, text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (book_id, p["paragraph_id"], p["spine_href"],
                     p["para_index"], p["tag"], p["text"])
                    for p in paragraphs
                ],
            )
    except Exception as exc:
        print(f"[parse] FAILED book={book_id}: {exc}")
        with get_conn() as conn:
            conn.execute("UPDATE books SET status='FAILED' WHERE id=?", (book_id,))


@router.get("")
def list_books():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, author, status, created_at FROM books ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{book_id}")
def get_book(book_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Book not found")
    return dict(row)


@router.get("/{book_id}/spine")
def get_spine(book_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT epub_path FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Book not found")
    book = epub_lib.read_epub(row["epub_path"], options={"ignore_ncx": True})
    spine = []
    for i, (idref, _) in enumerate(book.spine):
        item = book.get_item_with_id(idref)
        if item:
            spine.append({"index": i, "href": item.get_name()})
    return spine


@router.get("/{book_id}/item/{path:path}")
def get_epub_item(book_id: str, path: str):
    with get_conn() as conn:
        row = conn.execute("SELECT epub_path FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Book not found")
    book = epub_lib.read_epub(row["epub_path"], options={"ignore_ncx": True})
    item = book.get_item_with_href(path)
    if not item:
        raise HTTPException(404, f"Item not found: {path}")
    mime, _ = mimetypes.guess_type(path)
    return RawResponse(content=item.get_content(), media_type=mime or "application/octet-stream")


@router.get("/{book_id}/epub")
def get_epub_file(book_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT epub_path FROM books WHERE id=?", (book_id,)).fetchone()
    if not row or not os.path.exists(row["epub_path"]):
        raise HTTPException(404, "EPUB not found")
    return FileResponse(row["epub_path"], media_type="application/epub+zip")


@router.delete("/{book_id}")
def delete_book(book_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT epub_path FROM books WHERE id=?", (book_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Book not found")
        conn.execute("DELETE FROM bookmarks WHERE book_id=?", (book_id,))
        conn.execute("DELETE FROM paragraphs WHERE book_id=?", (book_id,))
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))

    book_dir = os.path.join(DATA_DIR, "books", book_id)
    cache_dir = os.path.join(DATA_DIR, "cache", book_id)
    for d in (book_dir, cache_dir):
        if os.path.exists(d):
            shutil.rmtree(d)

    return {"deleted": book_id}
