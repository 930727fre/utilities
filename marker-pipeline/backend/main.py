import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import storage
from routers import books

DATA_DIR = storage.DATA_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_DIR, exist_ok=True)
    # Conversions run as in-process background tasks; any book still marked
    # PARSING when this process starts is orphaned from a prior crash. Flip
    # to FAILED so the UI surfaces a red row instead of an eternal "Converting…".
    for book in storage.list_books():
        if book.get("status") == "PARSING":
            storage.update_book(book["id"], status="FAILED")
            print(f"[startup] orphaned PARSING book {book['id']} -> FAILED")
    yield


app = FastAPI(title="marker-pipeline", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(books.router)


@app.get("/health")
def health():
    return {"ok": True}
