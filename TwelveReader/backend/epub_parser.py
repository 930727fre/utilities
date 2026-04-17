import hashlib
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup


PARA_TAGS = {"p", "h1", "h2", "h3", "li"}


def make_paragraph_id(book_id: str, spine_href: str, tag: str, text: str, para_index: int) -> str:
    raw = f"{book_id}|{spine_href}|{tag}|{text[:100]}|{para_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_epub(epub_path: str, book_id: str) -> tuple[dict, list[dict]]:
    """Read EPUB once, return (metadata, paragraphs)."""
    book = epub.read_epub(epub_path, options={"ignore_ncx": True})

    title = book.get_metadata("DC", "title")
    author = book.get_metadata("DC", "creator")
    meta = {
        "title": title[0][0] if title else "Unknown Title",
        "author": author[0][0] if author else "Unknown Author",
    }

    paragraphs = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        spine_href = item.get_name()
        soup = BeautifulSoup(item.get_content(), "lxml")
        para_index = 0

        for elem in soup.find_all(list(PARA_TAGS)):
            text = elem.get_text(separator=" ", strip=True)
            if not text:
                continue
            pid = make_paragraph_id(book_id, spine_href, elem.name, text, para_index)
            paragraphs.append({
                "paragraph_id": pid,
                "spine_href": spine_href,
                "para_index": para_index,
                "tag": elem.name,
                "text": text,
            })
            para_index += 1

    return meta, paragraphs
