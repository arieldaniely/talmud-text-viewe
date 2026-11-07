"""Match explicit Sefaria editions to printed Rashi and Tosafot, offline in the viewer."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import gzip
import hashlib
import importlib
from itertools import groupby
import json
from pathlib import Path
import re
import unicodedata

from fontTools.ttLib import TTFont

from public_content import exclude_gra_notes

gemara = importlib.import_module('import-sefaria')
ROOT = gemara.ROOT
BACKUP = gemara.CACHE / 'before-commentary'
AUTHORS = {
    'Rashi': {'rashi', 'author-rashi'},
    'Tosafot': {'tosafot', 'author-tosafot', 'tosafot-continuation'},
}
EDITION = 'Vilna Edition'
WORD = re.compile(r'\S+')
HEBREW = re.compile('[^א-ת]')
MARKS = re.compile(r'[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]')
DIBBUR_SEPARATOR = re.compile(r'\s[-–—](?=\s|[א-ת]|$)|[–—](?=\s)')
LEADING_BOLD = re.compile(r'^\s*<(b|strong)\b[^>]*>(.*?)</\1>', re.I | re.S)
WINDOW = 5
MAX_GAP = 14
NEARBY_PAGES = 6
REMOTE_RUN = 10


@dataclass
class Token:
    start: int
    end: int
    key: str
    owner: int
    runs: tuple = ()


def key(text):
    return HEBREW.sub('', unicodedata.normalize('NFKD', text))


def source_text(html):
    return MARKS.sub('', gemara.plain(re.sub(r'<br\s*/?>', ' ', html, flags=re.I)))


def comment_text(html, author):
    text = source_text(html)
    marked = LEADING_BOLD.match(html)
    limit = min(len(text), 125)
    separator = DIBBUR_SEPARATOR.search(text, 0, limit)
    if separator and len(text[:separator.start()].split()) <= 19:
        head = text[:separator.start()].rstrip()
        if author == 'Rashi':
            head = head.rstrip('.:') + '.'
            body = text[separator.end():].lstrip()
            return head + (' '+body if body else ''), len(head)
        return text, len(head)
    if marked:
        return text, len(source_text(marked[2]))
    # Several Vilna transcriptions already use a period at the boundary.
    depth = 0
    for i,c in enumerate(text[:limit]):
        if c in '([':
            depth += 1
        elif c in ')]':
            depth = max(0, depth-1)
        elif not depth and c==':':
            break
        elif not depth and c=='.' and text[i+1:i+2]==' ':
            if len(text[:i].split()) <= 16:
                return text, i+1
            break
    return text, 0


def source_tokens(tree, author):
    comments, tokens = [], []

    def visit(value, daf, path):
        if isinstance(value, list):
            for index, child in enumerate(value, 1):
                visit(child, daf, [*path, index])
        elif isinstance(value, str):
            text, head_end = comment_text(value, author)
            if not text:
                return
            owner = len(comments)
            comments.append((':'.join([daf, *map(str, path)]), text, head_end))
            tokens.extend(Token(m.start(), m.end(), key(m.group()), owner) for m in WORD.finditer(text))
        else:
            raise ValueError(f'Invalid Sefaria commentary at {daf}:{path}')

    for index, section in enumerate(tree):
        visit(section, f'{index // 2 + 1}{"ab"[index % 2]}', [])
    return comments, tokens


def printed_tokens(streams):
    tokens = []
    for owner, (_, stream) in enumerate(streams):
        text, runs = stream['text'], stream['runs']
        ends = [r[0] + r[1] for r in runs]
        for word in WORD.finditer(text):
            first = bisect_right(ends, word.start())
            stop = first
            clean = []
            while stop < len(runs) and runs[stop][0] < word.end():
                start, length, *_, reference = runs[stop]
                if not reference:
                    clean.append(text[max(start, word.start()):min(start + length, word.end())])
                stop += 1
            eligible = (first < stop and runs[first][0] == word.start()
                        and ends[stop - 1] == word.end()
                        and all(ends[i-1] == runs[i][0] for i in range(first+1, stop))
                        and all(not r[6] for r in runs[first:stop]))
            tokens.append(Token(word.start(), word.end(), key(''.join(clean)), owner,
                                tuple(range(first, stop)) if eligible else ()))
    return tokens


def unique_phrases(keys):
    found = {}
    for start in range(len(keys) - WINDOW + 1):
        phrase = tuple(keys[start:start + WINDOW])
        if all(phrase) and sum(map(len, phrase)) >= 16:
            found[phrase] = start if phrase not in found else None
    return found


def anchor_pairs(local, source):
    left, right = unique_phrases(local), unique_phrases(source)
    votes = defaultdict(set)
    for phrase, a in left.items():
        b = right.get(phrase)
        if a is not None and b is not None:
            for delta in range(WINDOW):
                votes[a + delta].add(b + delta)
    pairs = [(a, next(iter(bs))) for a, bs in sorted(votes.items()) if len(bs) == 1]
    counts = Counter(b for _, b in pairs)
    return [(a, b) for a, b in pairs if counts[b] == 1]


def consecutive_anchors(anchors):
    # On a continuous diagonal both token positions advance by one.
    for _, entries in groupby(enumerate(anchors), lambda e: (e[1][0]-e[0], e[1][1]-e[0])):
        yield [pair for _, pair in entries]


def align(local, source, printed_dafs, source_dafs):
    """Yield disjoint intervals in printed order, permitting jumps in source order."""
    local_keys = [token.key for token in local]
    source_keys = [token.key for token in source]
    anchors = []
    for run in consecutive_anchors(anchor_pairs(local_keys, source_keys)):
        for a, b in run:
            printed = printed_dafs[local[a].owner]
            logical = source_dafs[source[b].owner]
            # Short phrases can be quotations from a distant passage.
            if abs(printed-logical) <= NEARBY_PAGES or len(run) >= REMOTE_RUN:
                anchors.append((a, b))
    if not anchors:
        return
    occupied = {b for _, b in anchors}
    gaps = []
    for (a, b), (end, last) in zip(anchors, anchors[1:]):
        a += 1
        b += 1
        if not (0 < end - a <= MAX_GAP and 0 < last - b <= MAX_GAP):
            continue
        if occupied.intersection(range(b, last)):
            continue
        left = local_keys[a:end]
        right = source_keys[b:last]
        similarity = SequenceMatcher(None, ''.join(left), ''.join(right), autojunk=False).ratio()
        if similarity < .64:
            # The words on either side are exact anchors, but the text between
            # them can be a genuine edition difference or a local insertion.
            continue
        if len(left) == len(right):
            gaps.extend((a+i, a+i+1, b+i, b+i+1) for i in range(len(left)))
        else:
            gaps.append((a, end, b, last))
    claims = Counter(index for _, _, start, end in gaps for index in range(start, end))
    yield from sorted([(a, a+1, b, b+1) for a, b in anchors] + [
        gap for gap in gaps if all(claims[i] == 1 for i in range(gap[2], gap[3]))
    ])


@lru_cache(maxsize=1)
def widths():
    font = TTFont(ROOT / 'fonts/mekorot-rashi.woff2')
    cmap, units = font.getBestCmap(), font['head'].unitsPerEm
    return {chr(code): font['hmtx'][glyph][0] / units for code, glyph in cmap.items()}


def patch_for(stream, printed, original, comments):
    if not all(t.runs for t in printed) or any(t.owner != printed[0].owner for t in printed):
        return None
    if any(t.owner != original[0].owner for t in original):
        return None
    run_ids = [i for token in printed for i in token.runs]
    if run_ids != list(range(run_ids[0], run_ids[-1] + 1)):
        return None
    runs = [stream['runs'][i] for i in run_ids]
    first, last = printed[0].start, printed[-1].end
    ref, comment, head_end = comments[original[0].owner]
    offset = original[0].start
    text = comment[offset:original[-1].end]
    if text == stream['text'][first:last]:
        return first, last, text, run_ids, [r[:] for r in runs], ref, offset, head_end
    left, right = min(r[2] for r in runs), max(r[3] for r in runs)
    y, size = runs[0][4:6]
    # A patch may occupy a single original line only. Mixed baselines can be
    # superscripts, opening words, or a change of column.
    if any(abs(r[4] - y) > .45 or abs(r[5] - size) > .6 for r in runs):
        return None
    if any(not -.12 <= a[2] - b[3] <= size * 1.2 for a, b in zip(runs, runs[1:])):
        return None
    metrics = widths()
    words = list(WORD.finditer(text))
    natural = [sum(metrics.get(c, .46) for c in w.group()) * size for w in words]
    gap = size * .2
    needed = sum(natural) + gap * (len(words) - 1)
    if not needed or needed > (right-left) / .68:
        return None
    scale = min(1., (right-left) / needed)
    x = right
    replacement = []
    for word, width in zip(words, natural):
        replacement.append([first+word.start(), len(word.group()), round(x-width*scale, 3),
                            round(x, 3), y, round(size*scale, 3), 0])
        x -= (width + gap) * scale
    return first, last, text, run_ids, replacement, ref, offset, head_end


def apply_patches(page, stream, patches, source):
    text, runs, parts, positioned = stream['text'], stream['runs'], [], []
    fragments = []
    headings = []
    cursor = run_cursor = shift = 0
    changes = []
    for start, end, value, indices, replacement, ref, offset, head_end in patches:
        parts.extend((text[cursor:start], value))
        positioned.extend([r[0]+shift, *r[1:]] for r in runs[run_cursor:indices[0]])
        positioned.extend([r[0]+shift, *r[1:]] for r in replacement)
        at = start + shift
        if (fragments and fragments[-1][2] == ref
                and at - (fragments[-1][0] + fragments[-1][1]) == 1
                and offset - (fragments[-1][3] + fragments[-1][1]) == 1
                and text[cursor:start] == ' '):
            fragments[-1][1] = at + len(value) - fragments[-1][0]
        else:
            fragments.append([at, len(value), ref, offset])
        if offset < head_end:
            length = min(len(value), head_end-offset)
            if headings and headings[-1][2]==ref and at-headings[-1][1]<=1:
                headings[-1][1] = at+length
            else:
                headings.append([at,at+length,ref])
        if text[start:end] != value:
            changes.append((start, end, len(value)))
        shift += len(value) - (end-start)
        cursor, run_cursor = end, indices[-1] + 1
    parts.append(text[cursor:])
    positioned.extend([r[0]+shift, *r[1:]] for r in runs[run_cursor:])

    boundaries = [end for _, end, _ in changes]
    shifts = [0]
    for start, end, size in changes:
        shifts.append(shifts[-1] + size - (end-start))

    def remap(position):
        index = bisect_right(boundaries, position)
        if index < len(changes):
            start, end, size = changes[index]
            if position > start:
                return start + shifts[index] + round((position-start) * size / (end-start))
        return position + shifts[index]

    for child in page['streams']:
        if child.get('parent') == stream['id'] and isinstance(child.get('at'), int):
            child['at'] = remap(child['at'])
    for figure in page['figures']:
        if figure['stream'] == stream['id'] and isinstance(figure.get('at'), int):
            figure['at'] = remap(figure['at'])
    if 'references' in stream:
        stream['references'] = [[remap(start), length] for start, length in stream['references']]
    if 'literalLatin' in stream:
        stream['literalLatin'] = [remap(start) for start in stream['literalLatin']]
    stream.update(text=''.join(parts), runs=positioned, source={**source, 'fragments': fragments})
    if headings:
        stream['headings'] = [[start,end-start] for start,end,_ in headings]


def fetch_text(work, edition):
    response = gemara.fetch('texts/'+work, {'pad':0,'commentary':0,'context':0,'vhe':edition,
                                           'fallbackOnDefaultVersion':0,'stripItags':1})
    if response.get('heVersionTitle') != edition:
        raise ValueError(f'Wrong edition for {work}')
    return response


def import_work(pages, title, author, place_illustrations=None):
    work = f'{author} on {title}'
    edition = 'WikiSource Rashi' if work == 'Rashi on Rosh Hashanah' else EDITION
    versions = gemara.fetch('texts/versions/'+work)
    meta = next((v for v in versions if v['versionTitle'] == edition and v['language'] == 'he'), None)
    if meta is None or meta.get('license') not in {'Public Domain', 'CC-BY-SA'}:
        raise ValueError(f'No approved edition for {work}')
    response = fetch_text(work, edition)
    comments, original = source_tokens(response['he'], author)
    streams = [(p, s) for p in pages for s in p['streams']
               if s['role'] == 'commentary' and s['id'] in AUTHORS[author]
               # The main printed slot switches from Rashi to Rashbam here.
               and (work != 'Rashi on Bava Batra' or p['n'] <= 55)]
    printed = printed_tokens(streams)
    printed_dafs = [gemara.printed_address(p['daf']) for p, _ in streams]
    source_dafs = []
    for ref, _, _ in comments:
        daf = ref.split(':', 1)[0]
        source_dafs.append(int(daf[:-1])*2-2+(daf[-1]=='b'))
    patches = defaultdict(list)
    imported = 0
    for a, end, b, last in align(printed, original, printed_dafs, source_dafs):
        owner = printed[a].owner
        patch = patch_for(streams[owner][1], printed[a:end], original[b:last], comments)
        if patch:
            patches[owner].append(patch)
            imported += end-a
    source = dict(provider='Sefaria', ref=work, versionTitle=edition,
                  license=meta['license'], versionSource=meta.get('versionSource'))
    for owner, edits in patches.items():
        page, stream = streams[owner]
        apply_patches(page, stream, edits, source)
    if place_illustrations:
        place_illustrations(pages, work, response, author)
    return {**source, 'streams': len(streams), 'updatedStreams': len(patches),
            'printedWords': len(printed), 'matchedWords': imported}


def restore_commentary(pages, original):
    """Reuse the first import's commentary geometry without resetting Gemara edits."""
    for page, before in zip(pages, original, strict=True):
        if page['n'] != before['n']:
            raise ValueError('Commentary backup page mismatch')
        ids = {s['id'] for s in page['streams'] if 'fragments' in s.get('source', {})}
        old = {s['id']: s for s in before['streams']}
        for index, stream in enumerate(page['streams']):
            if stream['id'] in ids or stream.get('parent') in ids:
                page['streams'][index] = old[stream['id']]
        old_figures = {(f['stream'], tuple(f['box'])): f['at'] for f in before['figures'] if 'at' in f}
        for figure in page['figures']:
            location = (figure['stream'], tuple(figure['box']))
            if figure['stream'] in ids and location in old_figures:
                figure['at'] = old_figures[location]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--book', type=int, action='append')
    args = parser.parse_args()
    illustration_import = importlib.import_module('import-illustrations') if args.apply else None
    catalog = json.loads((ROOT/'catalog.json').read_text(encoding='utf8'))
    changes = []
    report = {}
    for book in catalog:
        ident = book['id']
        if (args.book and ident not in args.book) or ident == 12:
            continue
        path = ROOT/book['path']
        packed = path.read_bytes()
        pages = json.loads(gzip.decompress(packed))
        backup = BACKUP / path.name
        if backup.exists():
            restore_commentary(pages, json.loads(gzip.decompress(backup.read_bytes())))
        elif any('fragments' in s.get('source', {}) for p in pages for s in p['streams']):
            raise ValueError(f'Original commentary required to rerun {path.name}')
        title = gemara.TITLES[ident]
        eligible = [p for p in pages if ident != 33 or p['n'] < 42]
        if illustration_import:
            upgrades = illustration_import.clear_placements(eligible, commentary_only=True)
        place = illustration_import.place_work if illustration_import else None
        stats = [import_work(eligible, title, author, place) for author in AUTHORS]
        if illustration_import:
            illustration_import.restore_upgrades(eligible,upgrades)
        for page in pages:
            exclude_gra_notes(page)
        report[str(ident)] = stats
        print(title, [(r['matchedWords'], r['printedWords']) for r in stats], flush=True)
        if args.apply:
            data = json.dumps(pages, ensure_ascii=False, separators=(',', ':')).encode()
            changes.append((book, path, backup, packed, gzip.compress(data, compresslevel=9, mtime=0),
                            sum(len(s['text']) for p in pages for s in p['streams'])))
    if args.apply:
        BACKUP.mkdir(parents=True, exist_ok=True)
        for book, path, backup, before, packed, chars in changes:
            if not backup.exists():
                backup.write_bytes(before)
            temp = path.with_suffix('.tmp')
            temp.write_bytes(packed)
            temp.replace(path)
            book.update(bytes=len(packed), sha256=hashlib.sha256(packed).hexdigest(), characters=chars)
        (ROOT/'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    report_path = ROOT/'qa/commentary-import.json' if args.apply else gemara.CACHE/'commentary-preview.json'
    if args.book and report_path.exists():
        report = {**json.loads(report_path.read_text(encoding='utf8')), **report}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')


if __name__ == '__main__':
    main()
