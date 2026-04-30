import hashlib
import html2text as _html2text
import json
import os
import posixpath
import re
import time
import zipfile
import xml.etree.ElementTree as ET

OLLAMA_URL = os.environ.get('TWELVEREADER_OLLAMA_URL', 'http://ollama:11434')
DATA_DIR = os.environ.get('DATA_DIR', '/data')
OLLAMA_MODEL = 'qwen2.5:14b'
PARAGRAPHS_PER_CHUNK = 3

_h2t = _html2text.HTML2Text()
_h2t.ignore_links = False
_h2t.body_width = 0
_h2t.ignore_images = False

_SYSTEM_PROMPT = (
    "You are a markdown linter. Fix syntax errors and output the corrected markdown. "
    "If there are no errors, output the markdown as-is. "
    "Do not add, remove, or rewrite any content."
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


def _fix_image_paths(md: str, chapter_zip_path: str, book_id: str) -> str:
    chapter_dir = posixpath.dirname(chapter_zip_path)

    def replace(match):
        alt, src = match.group(1), match.group(2)
        if src.startswith(('http://', 'https://', 'data:', '/')):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(chapter_dir, src)).lstrip('/')
        return f'![{alt}](/api/books/{book_id}/assets/{resolved})'

    return re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace, md)


def _chunk_md(md: str) -> list[str]:
    blocks = [b for b in md.split('\n\n') if b.strip()]
    chunks = []
    for i in range(0, len(blocks), PARAGRAPHS_PER_CHUNK):
        chunks.append('\n\n'.join(blocks[i:i + PARAGRAPHS_PER_CHUNK]))
    return chunks


def _ollama_clean_chunk(md: str, label: str, log) -> str:
    import httpx
    payload = {
        'model': OLLAMA_MODEL,
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': md},
        ],
        'stream': True,
        'options': {'num_ctx': 16384, 'temperature': 0},
    }
    parts = []
    # msg = f'[epub] {label} → Ollama...\n'
    # log.write(msg); log.flush()
    with httpx.stream('POST', f'{OLLAMA_URL}/v1/chat/completions',
                      json=payload, timeout=300.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith('data: ') or line == 'data: [DONE]':
                continue
            token = json.loads(line[6:])['choices'][0]['delta'].get('content', '')
            if token:
                parts.append(token)
                log.write(token); log.flush()
    result = ''.join(parts)
    # msg = f'[epub] {label} done ({len(result):,} chars)\n'
    # log.write(msg); log.flush()
    return result


def _clean_md(md: str, label: str, log) -> str:
    chunks = _chunk_md(md)
    if len(chunks) > 1:
        pass
        # msg = f'[epub] {label}: {len(md):,} chars → {len(chunks)} chunks\n'
        # log.write(msg); log.flush()
    results = []
    for i, chunk in enumerate(chunks):
        chunk_label = f'{label} chunk {i+1}/{len(chunks)}' if len(chunks) > 1 else label
        try:
            results.append(_ollama_clean_chunk(chunk, chunk_label, log))
        except Exception as exc:
            # msg = f'[epub] {chunk_label} Ollama failed, using html2text output: {exc}\n'
            # log.write(msg); log.flush()
            results.append(chunk)
    return '\n\n'.join(results)


def convert_epub(book_id: str, epub_path: str, book_dir: str) -> dict:
    with zipfile.ZipFile(epub_path) as z:
        z.extractall(book_dir)

    title, author, chapters = _parse_epub(epub_path)
    total = len(chapters)
    log_path = os.path.join(DATA_DIR, 'conversion.log')

    parts = []
    with open(log_path, 'w', encoding='utf-8') as log:
        for i, (html, chapter_path) in enumerate(chapters):
            label = f'ch {i+1}/{total}'
            rough = _h2t.handle(html).strip()
            # msg = f'[epub] {label}: {len(html):,} bytes → {len(rough):,} chars (html2text)\n'
            # log.write(msg); log.flush()
            md = _clean_md(rough, label, log)
            md = _fix_image_paths(md, chapter_path, book_id)
            parts.append(md)

    full_md = '\n\n'.join(parts)
    md_path = os.path.join(book_dir, f'{book_id}.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(full_md)

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
