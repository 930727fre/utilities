import hashlib
import json
import os
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup, NavigableString

OLLAMA_URL = os.environ.get('TWELVEREADER_OLLAMA_URL', 'http://ollama:11434')
DATA_DIR = os.environ.get('DATA_DIR', '/data')
OLLAMA_MODEL = 'qwen2.5:14b'

_CONTENT_BLOCKS = {
    'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'ul', 'ol', 'blockquote', 'pre', 'figure', 'img',
}
_CONTAINERS = {
    'div', 'section', 'article', 'main', 'aside',
    'header', 'footer', 'body', 'html',
}

_SYSTEM_PROMPT = (
    "Convert this HTML element to markdown. "
    "Preserve all images using markdown syntax: ![alt attribute](images/filename) where filename is only the filename from the src attribute. "
    "Remove superscript footnote markers (e.g. ¹, ², [1], [2]) but preserve all other text content. "
    "Output only the markdown, nothing else."
)


def _parse_epub(epub_path: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Returns (title, author, [(chapter_html, chapter_zip_path), ...]) in spine order."""
    with zipfile.ZipFile(epub_path) as z:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        opf_path = container.find(
            './/{urn:oasis:names:tc:opendocument:xmlns:container}rootfile'
        ).get('full-path')
        opf_dir = os.path.dirname(opf_path)

        opf = ET.fromstring(z.read(opf_path))
        dc = 'http://purl.org/dc/elements/1.1/'
        opf_ns = 'http://www.idpf.org/2007/opf'

        title = opf.findtext(f'.//{{{dc}}}title') or 'Unknown Title'
        author = opf.findtext(f'.//{{{dc}}}creator') or 'Unknown Author'

        manifest = {
            item.get('id'): item.get('href')
            for item in opf.findall(f'.//{{{opf_ns}}}item')
            if 'nav' not in item.get('properties', '')
        }

        chapters = []
        for itemref in opf.findall(f'.//{{{opf_ns}}}itemref'):
            if itemref.get('linear') == 'no':
                continue
            href = manifest.get(itemref.get('idref'), '')
            if not href.endswith(('.html', '.xhtml', '.htm')):
                continue
            full = '/'.join(filter(None, [opf_dir, href]))
            try:
                html = z.read(full).decode('utf-8', errors='replace')
                chapters.append((html, full))
            except KeyError:
                pass

        return title, author, chapters


_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp'}


def _extract_images(epub_path: str, book_dir: str) -> None:
    images_dir = os.path.join(book_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)
    with zipfile.ZipFile(epub_path) as z:
        for name in z.namelist():
            if os.path.splitext(name)[1].lower() in _IMAGE_EXTENSIONS:
                filename = os.path.basename(name)
                with z.open(name) as src:
                    with open(os.path.join(images_dir, filename), 'wb') as dst:
                        dst.write(src.read())


def _extract_blocks(html: str) -> list[str]:
    """Extract block-level elements from XHTML in document order."""
    soup = BeautifulSoup(html, 'html.parser')
    body = soup.find('body') or soup
    blocks = []

    def traverse(el):
        if isinstance(el, NavigableString):
            return
        if el.name in _CONTENT_BLOCKS:
            blocks.append(str(el))
        elif el.name in _CONTAINERS:
            for child in el.children:
                traverse(child)

    traverse(body)
    return blocks



def _ollama_call(messages: list, stream: bool, log=None, temperature: float = 0.2) -> str:
    import httpx
    payload = {
        'model': OLLAMA_MODEL,
        'messages': messages,
        'stream': stream,
        'options': {'num_ctx': 16384, 'temperature': temperature},
    }
    if stream:
        parts = []
        with httpx.stream('POST', f'{OLLAMA_URL}/v1/chat/completions',
                          json=payload, timeout=300.0) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith('data: ') or line == 'data: [DONE]':
                    continue
                token = json.loads(line[6:])['choices'][0]['delta'].get('content', '')
                if token:
                    parts.append(token)
                    if log:
                        log.write(token); log.flush()
        return ''.join(parts).strip()
    else:
        r = httpx.post(f'{OLLAMA_URL}/v1/chat/completions',
                       json=payload, timeout=60.0)
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()


def _is_pure_markdown(md: str) -> bool:
    examples = (
        "Is the following text vanilla markdown? Answer only True or False.\n\n"
        "<<<\n# Foreword\n>>>\nTrue\n\n"
        "<<<\n**by Nassim Nicholas Taleb**\n>>>\nTrue\n\n"
        "<<<\nLet us follow the logic of things from the beginning.\n>>>\nTrue\n\n"
        "<<<\n<p>Hello world</p>\n>>>\nFalse\n\n"
        "<<<\nSee Figure 3.right\n>>>\nFalse\n\n"
        f"<<<\n{md}\n>>>\n"
    )
    answer = _ollama_call([
        {'role': 'system', 'content': 'You are a markdown validator. Answer only True or False.'},
        {'role': 'user', 'content': examples},
    ], stream=False, temperature=0)
    return answer.lower().startswith('true')


def _ollama_convert_block(html_block: str, log) -> str:
    messages = [
        {'role': 'system', 'content': _SYSTEM_PROMPT},
        {'role': 'user', 'content': html_block},
    ]
    result = _ollama_call(messages, stream=True, log=log)
    log.write('\n\n'); log.flush()
    return result


def _fallback_convert(html_block: str) -> str:
    soup = BeautifulSoup(html_block, 'html.parser')
    tag = soup.find()
    if not tag:
        return ''
    if tag.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
        level = int(tag.name[1])
        return '#' * level + ' ' + tag.get_text().strip()
    if tag.name in ('img', 'figure'):
        img = tag.find('img') if tag.name == 'figure' else tag
        if img:
            src = posixpath.basename(img.get('src', ''))
            alt = img.get('alt', '') or (tag.find('figcaption') and tag.find('figcaption').get_text().strip()) or ''
            return f'![{alt}](images/{src})'
        return ''
    return tag.get_text(separator=' ').strip()


def convert_epub(book_id: str, epub_path: str, book_dir: str) -> dict:
    _extract_images(epub_path, book_dir)
    title, author, chapters = _parse_epub(epub_path)
    total = len(chapters)
    log_path = os.path.join(DATA_DIR, 'conversion.log')
    md_path = os.path.join(book_dir, f'{book_id}.md')

    with open(log_path, 'w', encoding='utf-8') as log, \
         open(md_path, 'w', encoding='utf-8') as md_file:
        for i, (html, _) in enumerate(chapters):
            blocks = _extract_blocks(html)
            chapter_parts = []
            for block in blocks:
                try:
                    md_block = _ollama_convert_block(block, log)
                except Exception:
                    md_block = _fallback_convert(block)
                if md_block:
                    chapter_parts.append(md_block)
            chapter_md = '\n\n'.join(chapter_parts)
            if i > 0:
                md_file.write('\n\n')
            md_file.write(chapter_md)
            md_file.flush()

    return {'meta': {'title': title, 'author': author}}


def paragraph_id(book_id: str, index: int, text: str) -> str:
    raw = f"{book_id}|{index}|{text[:100]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_md(book_dir: str, book_id: str) -> str:
    md_path = os.path.join(book_dir, f'{book_id}.md')
    with open(md_path, encoding='utf-8') as f:
        return f.read()


def split_paragraphs(md: str) -> list[str]:
    return [b.strip() for b in re.split(r'\n{2,}', md) if b.strip()]
