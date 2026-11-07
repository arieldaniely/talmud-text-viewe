"""Keep excluded source material out of the published data files."""

import gzip
import hashlib
import json
from pathlib import Path
import re


GRA_HEADING = 'הגהותהגרא'
GRA_STREAM = f'note-{GRA_HEADING}'
NON_HEBREW = re.compile('[^א-ת]')


def is_gra_stream(ident):
    return bool(ident) and (ident == GRA_STREAM or ident.startswith(GRA_STREAM + '-'))


def has_gra_heading(value):
    return GRA_HEADING in NON_HEBREW.sub('', value)


def contains_excluded_heading(stream):
    text = NON_HEBREW.sub('', stream.get('text', ''))
    if stream.get('role') == 'reference-note':
        return text.startswith(GRA_HEADING)
    return GRA_HEADING in text


def exclude_gra_notes(page):
    """Drop separated notes and refuse a page with a heading still in its text."""
    excluded = {stream['id'] for stream in page['streams'] if is_gra_stream(stream['id'])}
    while True:
        children = {s['id'] for s in page['streams'] if s.get('parent') in excluded}
        if children <= excluded:
            break
        excluded.update(children)
    kept = [stream for stream in page['streams'] if stream['id'] not in excluded]
    if any(
        is_gra_stream(stream.get('parent', ''))
        or has_gra_heading(stream.get('label', ''))
        or contains_excluded_heading(stream)
        for stream in kept
    ):
        raise ValueError(f"Unseparated excluded note on page {page['n']}; run source note repair")
    if excluded:
        page['streams'] = kept
        page['figures'] = [figure for figure in page['figures'] if figure['stream'] not in excluded]
    return len(excluded)


def verify_public_corpus(root):
    catalog = json.loads((root / 'catalog.json').read_text(encoding='utf8'))
    checked = 0
    files = set(root.glob('data/*.json.gz'))
    if files != {root / book['path'] for book in catalog}:
        raise ValueError('Public data files and catalog differ')
    for book in catalog:
        packed = (root / book['path']).read_bytes()
        if len(packed) != book['bytes'] or hashlib.sha256(packed).hexdigest() != book['sha256']:
            raise ValueError(f"Data and catalog checksum differ for {book['path']}")
        pages = json.loads(gzip.decompress(packed))
        if len(pages) != book['pages']:
            raise ValueError(f"Wrong number of pages in {book['path']}")
        for page in pages:
            try:
                if exclude_gra_notes(page):
                    raise ValueError(f"Excluded note present on page {page['n']}")
            except ValueError as error:
                raise ValueError(f"{book['title']}: {error}") from error
            checked += 1
    return checked


if __name__ == '__main__':
    print(f'Checked {verify_public_corpus(Path(__file__).resolve().parents[1])} public pages')
