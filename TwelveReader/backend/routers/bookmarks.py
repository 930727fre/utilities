from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import storage

router = APIRouter(prefix="/api/books", tags=["bookmarks"])


class BookmarkBody(BaseModel):
    paragraph_index: int


@router.get("/{book_id}/bookmark")
def get_bookmark(book_id: str):
    book = storage.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    idx = book.get("bookmark_paragraph_index")
    if idx is None:
        return None
    return {"paragraph_index": idx}


@router.put("/{book_id}/bookmark")
def put_bookmark(book_id: str, body: BookmarkBody):
    if not storage.get_book(book_id):
        raise HTTPException(404, "Book not found")
    storage.update_book(book_id, bookmark_paragraph_index=body.paragraph_index)
    return {"ok": True}
