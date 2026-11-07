"""Keep excluded source material out of the published data files."""

import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


GRA_HEADING = 'הגהותהגרא'
GRA_STREAM = f'note-{GRA_HEADING}'
NON_HEBREW = re.compile('[^א-ת]')
ILLUSTRATION_PREFIXES = {
    'data:image/png;base64': b'\x89PNG\r\n\x1a\n',
    'data:image/jpeg;base64': b'\xff\xd8\xff',
}


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
        if 'illustrations' in page:
            page['illustrations'] = [image for image in page['illustrations'] if image['stream'] not in excluded]
    return len(excluded)


def licensed_illustration(image):
    license = image.get('fileSource',{}).get('license','')
    return license.startswith(('CC BY','CC0','Public domain','PD')) and 'NC' not in license and 'ND' not in license


def verify_illustrations(page, seen, require_licenses=False):
    streams = {stream['id']: stream for stream in page['streams']}
    images = [(image, slot) for slot in page['figures'] for image in slot.get('images', [])]
    images.extend((image, None) for image in page.get('illustrations', []))
    for image, slot in images:
        key = image['ref'], image.get('edition'), image['index']
        if require_licenses and not licensed_illustration(image):
            raise ValueError(f'Illustration without a verified file license: {key}')
        if key in seen:
            raise ValueError(f'Duplicate Sefaria illustration: {key}')
        seen.add(key)
        header, encoded = image['src'].split(',', 1)
        if header not in ILLUSTRATION_PREFIXES or not base64.b64decode(encoded, validate=True).startswith(ILLUSTRATION_PREFIXES[header]):
            raise ValueError(f'Invalid Sefaria illustration bytes: {key}')
        width, height = image['size']
        if not (width > 0 and height > 0):
            raise ValueError(f'Invalid Sefaria illustration size: {key}')
        if 'crop' in image:
            x,y,w,h = image['crop']
            full_width,full_height = image['fileSize']
            if [w,h]!=image['size'] or not (0<=x<x+w<=full_width and 0<=y<y+h<=full_height):
                raise ValueError(f'Invalid Wikisource illustration crop: {key}')
            if 'clip' in image and not all(x<=a<=x+w and y<=b<=y+h for a,b in image['clip']):
                raise ValueError(f'Invalid Wikisource illustration mask: {key}')
        if credit:=image.get('fileSource'):
            origin = urlsplit(credit['file'])
            if (origin.scheme!='https' or origin.hostname not in {'commons.wikimedia.org','he.wikisource.org'}
                    or not credit.get('license') or not re.fullmatch('[0-9a-f]{64}',image.get('originalSha256',''))):
                raise ValueError(f'Invalid Wikisource illustration attribution: {key}')
        at = image['at']
        if at is not None and not (0 <= at <= len(streams[image['stream']]['text'])):
            raise ValueError(f'Invalid Sefaria illustration text anchor: {key}')
        if 'box' in image:
            x, y, w, h = image['box']
            if not (0 <= x < x+w <= 1.0001 and 0 <= y < y+h <= 1.0001):
                raise ValueError(f'Invalid Sefaria illustration page bounds: {key}')
        if slot is not None:
            sx, sy, sw, sh = slot['box']
            if 'box' not in image or image['stream'] != slot['stream'] or not (
                    sx-.00001 <= x < x+w <= sx+sw+.00001
                    and sy-.00001 <= y < y+h <= sy+sh+.00001):
                raise ValueError(f'Sefaria illustration exceeds its slot: {key}')
    for slot in page['figures']:
        boxes = [image['box'] for image in slot.get('images', [])]
        for i,a in enumerate(boxes):
            for b in boxes[i+1:]:
                separated = (a[0]+a[2]<=b[0]+.00001 or b[0]+b[2]<=a[0]+.00001
                             or a[1]+a[3]<=b[1]+.00001 or b[1]+b[3]<=a[1]+.00001)
                if not separated:
                    raise ValueError(f'Overlapping images on page {page["n"]}')
        if slot.get('layout')!='pdf':
            for a,b in zip(boxes,boxes[1:]):
                if not (b[0]+b[2]<=a[0]+.00001 or a[1]+a[3]<=b[1]+.00001):
                    raise ValueError(f'Out-of-order images on page {page["n"]}')
    return len(images)


def verify_public_corpus(root, require_licenses=False):
    catalog = json.loads((root / 'catalog.json').read_text(encoding='utf8'))
    checked = 0
    images = set()
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
                verify_illustrations(page, images, require_licenses)
            except ValueError as error:
                raise ValueError(f"{book['title']}: {error}") from error
            checked += 1
    return checked


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish',action='store_true')
    args = parser.parse_args()
    print(f'Checked {verify_public_corpus(Path(__file__).resolve().parents[1],args.publish)} public pages')
