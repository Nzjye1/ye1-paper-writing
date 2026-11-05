#!/usr/bin/env python3
"""Safely run MedSci's macOS Word Compare helper in an isolated directory."""
import argparse
from collections import Counter
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def comment_signatures(path):
    w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    with zipfile.ZipFile(path) as archive:
        if 'word/comments.xml' not in archive.namelist():
            return Counter()
        root = ET.fromstring(archive.read('word/comments.xml'))
    return Counter((item.get(w + 'author', ''), ''.join(t.text or '' for t in item.iter(w + 't')))
                   for item in root.iter(w + 'comment'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--original', required=True)
    p.add_argument('--revised', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--author', default='Ye1')
    p.add_argument('--timeout', type=int, default=300)
    a = p.parse_args()
    original, revised, out = (Path(x).expanduser().resolve() for x in (a.original, a.revised, a.out))
    if sys.platform != 'darwin':
        raise SystemExit('This helper needs macOS and Microsoft Word; use a host Word tool on other systems.')
    if not original.is_file() or not revised.is_file():
        raise SystemExit('Original and revised DOCX must both exist.')
    if out.exists() or out in (original, revised):
        raise SystemExit('Output must be a new file, not an input or existing file.')
    if out.suffix.lower() != '.docx' or not out.parent.is_dir():
        raise SystemExit('Output must be a DOCX in an existing directory.')
    helper = Path(__file__).with_name('build_marked_manuscript.py')
    with tempfile.TemporaryDirectory(prefix='ye1-word-compare-') as tmp:
        candidate = Path(tmp) / 'marked.docx'
        cmd = [sys.executable, str(helper), '--original', str(original), '--revised', str(revised),
               '--out', str(candidate), '--author', a.author, '--timeout', str(a.timeout)]
        try:
            run = subprocess.run(cmd, text=True, capture_output=True, timeout=a.timeout + 60)
        except subprocess.TimeoutExpired:
            raise SystemExit('Word did not finish; no result was saved.')
        if run.returncode != 0 or not candidate.is_file():
            print(run.stdout, end='', file=sys.stderr)
            print(run.stderr, end='', file=sys.stderr)
            raise SystemExit('Compare or its verification failed; no result was saved.')
        try:
            before, after, result = (comment_signatures(path) for path in (original, revised, candidate))
            expected = before | after
            if result - expected:
                print('NOTE: Word Compare may have duplicated comments; inspect before delivery.')
            if expected - result:
                print('NOTE: Some existing comments may be missing; inspect before delivery.')
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            print('NOTE: Comment preservation could not be checked; inspect before delivery.')
        created = False
        try:
            with out.open('xb') as target, candidate.open('rb') as source:
                created = True
                shutil.copyfileobj(source, target)
        except Exception:
            if created:
                out.unlink(missing_ok=True)
            raise
        print(run.stdout, end='')
        print(f'Output: {out}')


if __name__ == '__main__':
    main()
