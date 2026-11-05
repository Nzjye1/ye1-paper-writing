#!/usr/bin/env python3
"""Extract page-numbered PDF text locally; image-only pages need OCR separately."""
import argparse
import json
from pathlib import Path

from pypdf import PdfReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    args = parser.parse_args()
    pdf = PdfReader(str(Path(args.input)))
    if pdf.is_encrypted and not pdf.decrypt(''):
        raise SystemExit('PDF is encrypted; obtain accessible user-authorized content.')
    pages = []
    for number, page in enumerate(pdf.pages, 1):
        text = page.extract_text() or ''
        pages.append({'page': number, 'text': text, 'needs_ocr': not text.strip()})
    print(json.dumps({'pages': pages,
                      'scope': 'Extracted text only; inspect figures, layout and scanned pages separately.'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
