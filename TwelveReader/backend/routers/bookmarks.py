from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import get_conn

router = APIRouter(prefix="/api/books", tags=["bookmarks"])


class BookmarkBody(BaseModel):
    paragraph_id: str


@router.get("/{book_id}/bookmark")
def get_bookmark(book_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT paragraph_id, updated_at FROM bookmarks WHERE book_id=?", (book_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "No bookmark")
    return dict(row)


@router.put("/{book_id}/bookmark")
def put_bookmark(book_id: str, body: BookmarkBody):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO bookmarks (book_id, paragraph_id, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(book_id) DO UPDATE SET
                 paragraph_id=excluded.paragraph_id,
                 updated_at=CURRENT_TIMESTAMP""",
            (book_id, body.paragraph_id),
        )
    return {"book_id": book_id, "paragraph_id": body.paragraph_id}
