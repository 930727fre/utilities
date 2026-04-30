import json
import os
import threading

DATA_DIR = os.environ.get('DATA_DIR', '/data')
BOOKSHELF_PATH = os.path.join(DATA_DIR, 'bookshelf.json')
_lock = threading.Lock()


def _read() -> list:
    if not os.path.exists(BOOKSHELF_PATH):
        return []
    with open(BOOKSHELF_PATH) as f:
        return json.load(f)


def _write(books: list) -> None:
    with open(BOOKSHELF_PATH, 'w') as f:
        json.dump(books, f, indent=2, ensure_ascii=False)


def list_books() -> list:
    with _lock:
        return _read()


def get_book(book_id: str) -> dict | None:
    with _lock:
        return next((b for b in _read() if b['id'] == book_id), None)


def add_book(book: dict) -> None:
    with _lock:
        books = _read()
        books.append(book)
        _write(books)


def update_book(book_id: str, **fields) -> None:
    with _lock:
        books = _read()
        for b in books:
            if b['id'] == book_id:
                b.update(fields)
                break
        _write(books)


def remove_book(book_id: str) -> None:
    with _lock:
        books = [b for b in _read() if b['id'] != book_id]
        _write(books)
