"""
Document conversion pipeline — marker subprocess + per-book folder normalization.

`marker_single` writes `<basename>/<basename>.md`, `<basename>_meta.json`, and
extracted image files inside `--output_dir`. This module shells out to it once
per upload, then flattens marker's subdirectory into the book folder:

    /data/{book_id}/
        {book_id}.{epub|pdf}    ← original source
        {book_id}.md            ← converted markdown (anchor scaffolding stripped)
        meta.json               ← marker's metadata (TOC, page stats)
        _page_*.png / .jpg ...  ← extracted figures, flat alongside the md

The whole folder is later zipped on demand and returned to the user.
"""

import re
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ACCEPTED_EXTENSIONS = {".epub", ".pdf"}

# Process-wide lock so concurrent uploads serialize the GPU-bound marker call
# instead of fighting for VRAM. The flattening logic after marker exits is fast
# and runs unlocked.
_marker_lock = threading.Lock()


def _extract_pdf_meta(path: Path) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        m = reader.metadata or {}
        return {
            "title": (m.get("/Title") or "").strip(),
            "author": (m.get("/Author") or "").strip(),
        }
    except Exception as exc:
        print(f"[meta] pdf read failed: {exc}")
        return {"title": "", "author": ""}


def _extract_epub_meta(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(
                ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
            ).get("full-path")
            opf = ET.fromstring(z.read(opf_path))
            dc = "http://purl.org/dc/elements/1.1/"
            return {
                "title": (opf.findtext(f".//{{{dc}}}title") or "").strip(),
                "author": (opf.findtext(f".//{{{dc}}}creator") or "").strip(),
            }
    except Exception as exc:
        print(f"[meta] epub read failed: {exc}")
        return {"title": "", "author": ""}


def extract_meta(src_path: str) -> dict:
    """Read title/author from a PDF or EPUB. Empty strings on missing metadata."""
    p = Path(src_path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_meta(p)
    if suffix == ".epub":
        return _extract_epub_meta(p)
    return {"title": "", "author": ""}


_EMPTY_ANCHOR_SPAN_RE = re.compile(r'<span id="[^"]*"></span>')


def _scrub_anchors(md: str) -> str:
    """Drop marker's empty page-anchor spans.

    `<span id="page-N"></span>` is a navigation target for HTML-aware viewers;
    in plain-text or HTML-naive viewers it renders as literal escaped tags,
    which is just noise. Footnote-reference links like `[1](#page-N)` are kept
    so readers can still see where footnote markers sit in the body.
    """
    return _EMPTY_ANCHOR_SPAN_RE.sub("", md)


def convert_file(book_id: str, src_path: str, book_dir: str) -> None:
    """Run marker on `src_path`, flatten output under `book_dir`."""
    src = Path(src_path)
    book_dir_p = Path(book_dir)
    book_dir_p.mkdir(parents=True, exist_ok=True)

    with _marker_lock:
        subprocess.run(
            [
                "marker_single", str(src),
                "--output_dir", str(book_dir_p),
                "--output_format", "markdown",
            ],
            check=True,
        )

    marker_subdir = book_dir_p / src.stem
    if not marker_subdir.is_dir():
        raise RuntimeError(f"marker did not produce expected subdir {marker_subdir}")

    md_src = marker_subdir / f"{src.stem}.md"
    meta_src = marker_subdir / f"{src.stem}_meta.json"
    md_dst = book_dir_p / f"{book_id}.md"
    meta_dst = book_dir_p / "meta.json"

    for item in marker_subdir.iterdir():
        if item == md_src:
            md_dst.write_text(
                _scrub_anchors(item.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            item.unlink()
        elif item == meta_src:
            shutil.move(str(item), str(meta_dst))
        else:
            shutil.move(str(item), str(book_dir_p / item.name))

    shutil.rmtree(marker_subdir)
