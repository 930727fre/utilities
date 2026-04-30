"""Quick test: convert one EPUB chapter (or test_data.txt) via Gemini.

Usage:
  python test_gemini.py                        # reads test_data.txt
  python test_gemini.py book.epub [chapter#]   # extracts chapter N (default 0) from EPUB
  python test_gemini.py book.epub --size       # print before/after sizes for all chapters
  python test_gemini.py --raw                  # dump raw html without calling Gemini
"""
import sys

from epub_parser import _SYSTEM_PROMPT, GEMINI_API_KEY, _parse_epub, _strip_attributes


def print_size_report(chapters: list[str]) -> None:
    total_before = total_after = 0
    for i, html in enumerate(chapters):
        stripped = _strip_attributes(html)
        before, after = len(html.encode()), len(stripped.encode())
        saved = before - after
        pct = saved / before * 100 if before else 0
        print(f"  chapter {i:2d}: {before:>8,} → {after:>8,} bytes  (-{saved:,}, -{pct:.1f}%)")
        total_before += before
        total_after += after
    total_saved = total_before - total_after
    total_pct = total_saved / total_before * 100 if total_before else 0
    print(f"  {'TOTAL':>10}: {total_before:>8,} → {total_after:>8,} bytes  (-{total_saved:,}, -{total_pct:.1f}%)")


def main():
    if len(sys.argv) >= 2 and sys.argv[1].endswith('.epub'):
        epub_path = sys.argv[1]
        chapter_idx = int(sys.argv[2]) if len(sys.argv) >= 3 and sys.argv[2].isdigit() else 0
        _, _, chapters = _parse_epub(epub_path)

        if "--size" in sys.argv:
            print(f"Size report for {epub_path} ({len(chapters)} chapters):")
            print_size_report(chapters)
            return

        html = chapters[chapter_idx]
        print(f"Extracted chapter {chapter_idx} from {epub_path} ({len(html):,} bytes)")
    else:
        with open("test_data.txt", encoding="utf-8") as f:
            html = f.read()
        print(f"Read {len(html):,} bytes from test_data.txt")

        if "--size" in sys.argv:
            print("Size report:")
            print_size_report([html])
            return

    if "--raw" in sys.argv:
        print("\n--- RAW ---\n")
        print(html)
        return

    if not GEMINI_API_KEY:
        print("TWELVEREADER_GEMINI_API_KEY is not set")
        sys.exit(1)

    from google import genai
    html = _strip_attributes(html)
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("Calling Gemini...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"{_SYSTEM_PROMPT}\n\n{html}",
    )
    print("\n--- OUTPUT ---\n")
    print(response.text)


if __name__ == "__main__":
    main()
