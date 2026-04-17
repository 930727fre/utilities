from fastapi import APIRouter, HTTPException
from database import get_conn

router = APIRouter(prefix="/api/books", tags=["paragraphs"])


@router.get("/{book_id}/paragraphs")
def get_paragraphs(book_id: str):
    with get_conn() as conn:
        book = conn.execute("SELECT id FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            raise HTTPException(404, "Book not found")
        rows = conn.execute(
            """SELECT paragraph_id, spine_href, para_index, tag, text
               FROM paragraphs WHERE book_id=?
               ORDER BY id""",
            (book_id,),
        ).fetchall()
    return [dict(r) for r in rows]
