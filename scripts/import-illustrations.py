"""Place Sefaria illustrations using aligned text and original page geometry."""

import argparse
import base64
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from functools import lru_cache
import gzip
import hashlib
from html.parser import HTMLParser
import importlib
from io import BytesIO
import json
import re

from PIL import Image

commentary = importlib.import_module('import-commentary')
gemara = commentary.gemara
ROOT = gemara.ROOT
IMG_TAG = re.compile(r'<img\b[^>]*>', re.I)
HEBREW_CHAR = re.compile('[א-ת]')
DATA_IMAGE = re.compile(r'data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)')
WIKISOURCE = 'Wikisource Talmud Bavli'
GEMARA_SLOTS = ROOT/'qa/gemara-illustration-slots.json'
PDF_LAYOUTS = ROOT/'qa/pdf-illustration-layouts.json'


class ImageTag(HTMLParser):
    src = None

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.src = dict(attrs).get('src')


def image_data(tag):
    parser = ImageTag()
    parser.feed(tag)
    match = DATA_IMAGE.fullmatch(parser.src or '')
    if not match:
        raise ValueError('Expected embedded PNG or JPEG from Sefaria')
    with Image.open(BytesIO(base64.b64decode(match[2], validate=True))) as image:
        if image.format.lower() != match[1]:
            raise ValueError('Image bytes do not match the Sefaria media type')
        width, height = image.size
        image.verify()
    return {'src': parser.src, 'size': [width, height]}


def source_images(tree, author=None):
    def visit(value, daf, path):
        if isinstance(value, list):
            for i, child in enumerate(value, 1):
                yield from visit(child, daf, (*path, i))
        elif isinstance(value, str):
            for i, tag in enumerate(IMG_TAG.finditer(value), 1):
                prefix_html = value[:tag.start()]
                prefix = (commentary.comment_text(prefix_html, author)[0] if author
                          else commentary.source_text(prefix_html))
                suffix = commentary.source_text(value[tag.end():])
                ref = ':'.join([daf, *map(str, path)])
                yield ref, i, len(prefix), prefix, suffix, image_data(tag.group())

    for i, section in enumerate(tree):
        yield from visit(section, f'{i//2+1}{"ab"[i%2]}', ())


def logical_address(ref):
    daf = ref.split(':', 1)[0]
    return int(daf[:-1])*2-2+(daf[-1]=='b')


def fragment_anchor(fragments, offset):
    candidates = []
    for page, stream, start, length, source_start in fragments:
        end = source_start + length
        mapped = max(source_start, min(offset, end))
        distance = abs(offset-mapped)
        if distance <= 90:
            candidates.append((distance, mapped > offset, page, stream, start+mapped-source_start))
    if candidates:
        distance, after, page, stream, at = min(candidates, key=lambda x: x[:2])
        return page, stream, at, 'fragment'
    return None


def context_anchor(nearby, prefix, suffix, normalized, sizes=(28,20,15)):
    left = commentary.key(prefix[-350:])
    right = commentary.key(suffix[:350])
    for size in sizes:
        found = set()
        for page, stream in nearby:
            letters, offsets = normalized(page, stream)
            for phrase, before in ((left[-size:], True), (right[:size], False)):
                if len(phrase) < size:
                    continue
                for m in re.finditer(re.escape(phrase), letters):
                    at = offsets[m.end()-1]+1 if before else offsets[m.start()]
                    found.add((page, stream, at))
        # Context on both sides may locate opposite sides of intervening whitespace.
        if found:
            page, stream, at = min(found)
            if all(p == page and s == stream and abs(offset-at) <= 6
                   for p, s, offset in found):
                return page, stream, at, 'context'
    return None


def dimensions(box, page):
    return [v*(page['width'] if i%2==0 else page['height']) for i, v in enumerate(box)]


def anchor_run(stream, at):
    return min((r for r in stream['runs'] if not r[6]), key=lambda r: (
        min(abs(at-r[0]), abs(at-r[0]-r[1])), r[0] > at
    ), default=None)


def slot_cost(slot, image, stream, page):
    run = anchor_run(stream, image['at'])
    if run is None:
        return float('inf')
    left, top, width, height = dimensions(slot['box'], page)
    x, y = run[2], run[4]
    vertical = max(top-y, y-(top+height), 0)
    horizontal = max(left-x, x-(left+width), 0)
    difference = abs(slot['at']-image['at'])
    if vertical > 95 or (difference > 250 and vertical > 22):
        return float('inf')
    return vertical/20 + horizontal/100 + min(difference/100, 2.9)


def assign_slots(slots, images, stream, page):
    """Prefer physical anchors, keeping image order and allowing a shared box."""
    costs = [[slot_cost(slot, image, stream, page) for image in images] for slot in slots]

    @lru_cache(maxsize=None)
    def plan(i, j):
        if j == len(images):
            return 0, ()
        if i == len(slots):
            return 3.6*(len(images)-j), ()
        skipped_image, matches = plan(i, j+1)
        choices = [(3.6+skipped_image, matches), plan(i+1, j)]
        for end in range(j+1, min(j+4, len(images))+1):
            if images[end-1]['at']-images[j]['at'] > 290:
                break
            rest, matches = plan(i+1, end)
            cost = sum(costs[i][j:end]) + .48*(end-j-1)
            choices.append((cost+rest, ((i,j,end), *matches)))
        return min(choices, key=lambda choice: choice[0])

    return plan(0,0)[1]


def layout_images(box, images, page):
    """Fit each image at its own aspect ratio, in right-to-left or top-down order."""
    left, top, width, height = dimensions(box, page)
    if len(images) == 1:
        return [box]
    ratios = [image['size'][0]/image['size'][1] for image in images]
    gap = min(3, width*.05, height*.05)
    gaps = gap*(len(images)-1)
    row_height = min(height, (width-gaps)/sum(ratios))
    column_width = min(width, (height-gaps)/sum(1/r for r in ratios))
    boxes = []
    if row_height**2*sum(ratios) >= column_width**2*sum(1/r for r in ratios):
        x = left+(width+row_height*sum(ratios)+gaps)/2
        for ratio in ratios:
            w = row_height*ratio
            boxes.append([x-w, top+(height-row_height)/2, w, row_height])
            x -= w+gap
    else:
        y = top+(height-column_width*sum(1/r for r in ratios)-gaps)/2
        for ratio in ratios:
            h = column_width/ratio
            boxes.append([left+(width-column_width)/2, y, column_width, h])
            y += h+gap
    return [[round(v/(page['width'] if i%2==0 else page['height']), 6)
             for i, v in enumerate(coords)] for coords in boxes]


def free_box(page, stream, image):
    """Use nearby whitespace in the same column when no detected slot fits."""
    run = anchor_run(stream, image['at'])
    if run is None:
        return None
    x, y = run[2], run[4]
    column = [r for r in stream['runs'] if abs(r[4]-y) <= 32
              and r[2] <= x+85 and r[3] >= x-85]
    left, right = min(r[2] for r in column), max(r[3] for r in column)
    top, bottom = max(4,y-48), min(page['height']-4,y+58)
    obstacles = [
        (r[2]-1,r[4]-r[5],r[3]+1,r[4]+r[5]*.28)
        for s in page['streams'] for r in s['runs']
        if r[3]>=left-1 and r[2]<=right+1 and top-22<=r[4]<=bottom+22
    ]
    for item in (*page['figures'], *page.get('illustrations', [])):
        if 'box' in item:
            bx, by, w, h = dimensions(item['box'], page)
            if bx+w>=left and bx<=right and by+h>=top and by<=bottom:
                obstacles.append((bx-1,by-1,bx+w+1,by+h+1))
    pixel_w, pixel_h = image['size']
    best = None
    for edge in (88,72,60,50,42,34,28,23):
        scale = edge/max(pixel_w,pixel_h)
        w,h = pixel_w*scale,pixel_h*scale
        if min(w,h)<14 or w>right-left or h>bottom-top:
            continue
        xs = {left, right-w, x, x-w/2, x-w}
        ys = {top, bottom-h, y, y-h}
        for a,b,c,d in obstacles:
            xs.update((a-w,c))
            ys.update((b-h,d))
        xs = [xx for xx in xs if left<=xx<=right-w and xx<=x+28 and xx+w>=x-28]
        ys = [yy for yy in ys if top<=yy<=bottom-h and yy<=y+32 and yy+h>=y-32]
        for xx in xs:
            for yy in ys:
                if any(a<xx+w and xx<c and b<yy+h and yy<d for a,b,c,d in obstacles):
                    continue
                dx=max(xx-x,x-(xx+w),0)
                dy=max(yy-y,y-(yy+h),0)
                score=(edge-dx*.55-dy*.8, -abs(yy+h/2-y), -abs(xx+w/2-x))
                if best is None or score>best[0]:
                    best=score,[xx,yy,w,h]
    if best and best[0][0]>=13:
        return [round(v/(page['width'] if i%2==0 else page['height']),6)
                for i,v in enumerate(best[1])]
    return None


def place_work(pages, work, doc, author):
    assets = list(source_images(doc['he'], author))
    if not assets:
        return Counter()
    streams = {}
    all_commentaries = {}
    addresses = {}
    by_ref = defaultdict(list)
    for p, page in enumerate(pages):
        if not page['daf']:
            continue
        addresses[p] = gemara.printed_address(page['daf'])
        for s, stream in enumerate(page['streams']):
            if stream['role'] != 'commentary':
                continue
            all_commentaries[p,s] = stream
            if stream['id'] not in commentary.AUTHORS[author]:
                continue
            streams[p,s] = stream
            source = stream.get('source', {})
            if source.get('ref') == work:
                for at, length, ref, origin in source['fragments']:
                    by_ref[ref].append((p,s,at,length,origin))

    @lru_cache(maxsize=30)
    def normalized(p, s):
        letters = list(HEBREW_CHAR.finditer(all_commentaries[p,s]['text']))
        return ''.join(m.group() for m in letters), [m.start() for m in letters]

    anchored = defaultdict(list)
    for ref, index, offset, prefix, suffix, asset in assets:
        logical = logical_address(ref)
        position = fragment_anchor(by_ref.get(ref, ()), offset)
        if position is None:
            nearby = [(p,s) for p,s in streams if abs(addresses[p]-logical) <= 4]
            position = context_anchor(nearby, prefix, suffix, normalized)
        if position is None:
            # A printed column can contain a passage filed under another stream ID.
            nearby = [(p,s) for p,s in all_commentaries if (p,s) not in streams
                      and abs(addresses[p]-logical) <= 2]
            position = context_anchor(nearby,prefix,suffix,normalized,(34,28))
        if position is None:
            exact = [(p,s) for p,s in streams if addresses[p] == logical]
            if exact:
                p,s = exact[0]
            else:
                p = next(p for p in addresses if addresses[p] == logical)
                s = None
            position = p,s,None,'logical'
        p,s,at,method = position
        asset.update(ref=f'{work} {ref}', edition=doc['heVersionTitle'], index=index, at=at, method=method,
                     context=prefix[-110:].lstrip() or suffix[:110])
        anchored[p,s].append(asset)

    stats = Counter()
    for (p,s), images in anchored.items():
        label = 'רש״י' if author=='Rashi' else 'תוספות'
        stats.update(place_images(pages[p], all_commentaries.get((p,s)), images, label))
    return stats


def place_images(page, stream, images, label):
    stats = Counter()
    images.sort(key=lambda image: (image['at'] is None, image['at'] or 0))
    placed = set()
    gallery_only = set()
    if stream:
        placed,gallery_only,shared = place_reviewed(page,stream,images)
        stats['sharedSlots'] += shared
        slots = sorted((f for f in page['figures'] if f['stream']==stream['id'] and not f.get('images')),
                       key=lambda f:f['at'])
        candidates = [image for image in images if image['at'] is not None
                      and id(image) not in placed and id(image) not in gallery_only]
        for i,start,end in assign_slots(slots,candidates,stream,page):
            slot = slots[i]
            group = candidates[start:end]
            for image,box in zip(group,layout_images(slot['box'],group,page)):
                image['box'] = box
            slot['images'] = group
            placed.update(id(image) for image in group)
            stats['sharedSlots'] += len(group)>1
    for image in images:
        stats['images'] += 1
        stats[image['method']] += 1
        image['stream'] = stream['id'] if stream else None
        image['label'] = label
        if id(image) in placed:
            stats['inline'] += 1
        else:
            box = (free_box(page,stream,image) if stream and image['at'] is not None
                   and id(image) not in gallery_only else None)
            if box:
                image['box'] = box
                stats['inline'] += 1
                stats['nearAnchor'] += 1
            else:
                stats['belowPage'] += 1
            page.setdefault('illustrations', []).append(image)
    return stats


@lru_cache(maxsize=1)
def pdf_layouts():
    return json.loads(PDF_LAYOUTS.read_text(encoding='utf8'))


def place_reviewed(page,stream,images):
    by_ref = {(image['ref'],image['index']):image for image in images}
    placed = set()
    gallery_only = set()
    shared = 0
    for layout in pdf_layouts():
        if layout['page']!=page['n'] or layout['stream']!=stream['id']:
            continue
        if not all((ref,index) in by_ref for ref,index,_ in layout['images']):
            continue
        if layout.get('gallery'):
            gallery_only.update(id(by_ref[ref,index]) for ref,index,_ in layout['images'])
            continue
        box = layout['slot']
        slot = next((f for f in page['figures'] if f['stream']==stream['id'] and f['box']==box),None)
        if slot is None:
            if not layout.get('restore'):
                raise ValueError(f'Could not find audited PDF slot at page {page["n"]}: {box}')
            slot = {'stream':stream['id'],'at':0,'box':box,'restored':True}
            page['figures'].append(slot)
        slot['layout'] = 'pdf'
        slot['images'] = []
        x,y,w,h = box
        for ref,index,(left,top,width,height) in layout['images']:
            image = by_ref[ref,index]
            image['box'] = [round(v,6) for v in (x+left*w,y+top*h,width*w,height*h)]
            slot['images'].append(image)
            placed.add(id(image))
        shared += len(slot['images'])>1
    return placed,gallery_only,shared


@lru_cache(maxsize=1)
def gemara_slots():
    return json.loads(GEMARA_SLOTS.read_text(encoding='utf8'))


def gemara_documents(title, edition):
    yield commentary.fetch_text(title,edition)
    try:
        yield commentary.fetch_text(title,WIKISOURCE)
    except ValueError as error:
        if str(error) != f'Wrong edition for {title}':
            raise


def segment_anchor(stream, primary, ref, prefix, suffix):
    parts = ref.split(':')
    if len(parts)!=2:
        return None
    segments = primary['he'][logical_address(ref)]
    index = int(parts[1])-1
    if not 0<=index<len(segments):
        return None
    segment = gemara.gemara_text([segments[index]])[0]
    if not segment or stream['text'].count(segment)!=1:
        return None
    letters = list(HEBREW_CHAR.finditer(segment))
    target = ''.join(m.group() for m in letters)
    left,right = commentary.key(prefix),commentary.key(suffix)
    if not target or not left:
        return None
    alignment = SequenceMatcher(None,left+right,target,autojunk=False)
    if alignment.ratio()<.68:
        return None
    pivot = len(left)
    before = after = 0
    mapped = None
    for block in alignment.get_matching_blocks():
        if not block.size:
            continue
        before += max(0,min(block.a+block.size,pivot)-block.a)
        after += max(0,block.a+block.size-max(block.a,pivot))
        if mapped is None and block.a+block.size>pivot:
            mapped = block.b+max(0,pivot-block.a)
    if before<12 or (right and (after<min(10,len(right)) or mapped is None)):
        return None
    start = stream['text'].index(segment)
    return start+letters[mapped].start() if mapped is not None else start+len(segment)


def place_gemara(book, pages):
    streams = {}
    editions = {}
    for page in pages:
        for stream in page['streams']:
            source = stream.get('source', {})
            if stream['id']=='gemara' and source.get('provider')=='Sefaria':
                title,daf = source['ref'].rsplit(' ',1)
                streams[title,daf] = page,stream
                editions[title] = source['versionTitle']
    slots = gemara_slots().get(str(book), {})
    stats = Counter()
    for title,edition in editions.items():
        by_page = defaultdict(list)
        seen = set()
        documents = list(gemara_documents(title,edition))
        for doc in documents:
            for ref,index,_,prefix,suffix,asset in source_images(doc['he']):
                daf = ref.split(':',1)[0]
                if (title,daf) not in streams:
                    continue
                page,stream = streams[title,daf]
                letters = list(HEBREW_CHAR.finditer(stream['text']))
                normalized = ''.join(m.group() for m in letters),[m.start() for m in letters]
                position = context_anchor([(0,0)],prefix,suffix,lambda *_:normalized)
                at = position[2] if position else segment_anchor(stream,documents[0],ref,prefix,suffix)
                key = daf,at,asset['src']
                if key in seen:
                    continue
                seen.add(key)
                asset.update(ref=f'{title} {ref}',edition=doc['heVersionTitle'],index=index,at=at,
                             method='context' if at is not None else 'logical',
                             context=prefix[-110:].lstrip() or suffix[:110])
                by_page[daf].append(asset)
        for daf,images in by_page.items():
            page,stream = streams[title,daf]
            for box in slots.get(str(page['n']), []):
                x,y,w,h = dimensions(box,page)
                run = min(stream['runs'],key=lambda r: (r[4]-y)**2+
                          max(r[2]-x-w,x-r[3],0)**2)
                page['figures'].append({'stream':'gemara','box':box,'at':run[0]+run[1],
                                        'restored':True})
            stats.update(place_images(page,stream,images,'גמרא'))
            page['figures'] = [f for f in page['figures'] if f['stream']!='gemara'
                               or not f.get('restored') or f.get('images')]
    return stats


def clear_placements(pages, *, gemara_only=False, commentary_only=False):
    def clear(stream):
        return (not commentary_only or stream!='gemara') and (not gemara_only or stream=='gemara')

    upgrades = {}
    for page in pages:
        for image in page_images(page):
            if clear(image['stream']) and image.get('fileSource'):
                upgrades[asset_key(image)] = {key:image[key] for key in
                    ('src','size','fileSource','originalSha256','crop','fileSize','clip') if key in image}
        kept = [image for image in page.get('illustrations',[]) if not clear(image['stream'])]
        if kept:
            page['illustrations'] = kept
        else:
            page.pop('illustrations',None)
        page['figures'] = [f for f in page['figures'] if not f.get('restored') or not clear(f['stream'])]
        for slot in page['figures']:
            if clear(slot['stream']):
                slot.pop('images',None)
                slot.pop('layout',None)
    return upgrades


def page_images(page):
    for slot in page['figures']:
        yield from slot.get('images',[])
    yield from page.get('illustrations',[])


def asset_key(asset):
    return asset['ref'],asset.get('edition'),asset['index']


def restore_upgrades(pages, upgrades):
    if not upgrades:
        return
    for page in pages:
        for image in page_images(page):
            upgrade = upgrades.get(asset_key(image))
            if upgrade and hashlib.sha256(image['src'].encode()).hexdigest()==upgrade['originalSha256']:
                image.update(upgrade)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--book', type=int, action='append')
    args = parser.parse_args()
    catalog_path = ROOT/'catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf8'))
    reports = json.loads((ROOT/'qa/commentary-import.json').read_text(encoding='utf8'))
    stats = Counter()
    changes = []
    for book in catalog:
        ident = book['id']
        if args.book and ident not in args.book:
            continue
        works = []
        for source in reports.get(str(ident), []):
            work = source['ref']
            doc = commentary.fetch_text(work, source['versionTitle'])
            if any(source_images(doc['he'])):
                works.append((work, doc))
        path = ROOT/book['path']
        pages = json.loads(gzip.decompress(path.read_bytes()))
        upgrades = clear_placements(pages)
        found = place_gemara(ident,pages)
        if found:
            stats.update(found)
            print(gemara.TITLES[ident],dict(found),flush=True)
        for work, doc in works:
            found = place_work(pages, work, doc, work.split(' on ',1)[0])
            stats.update(found)
            print(work,dict(found),flush=True)
        restore_upgrades(pages,upgrades)
        if args.apply and (works or found):
            data = json.dumps(pages,ensure_ascii=False,separators=(',', ':')).encode()
            changes.append((book,path,gzip.compress(data,compresslevel=9,mtime=0)))
    if args.apply:
        for book,path,packed in changes:
            temp = path.with_suffix('.tmp')
            temp.write_bytes(packed)
            temp.replace(path)
            book.update(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())
        catalog_path.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(dict(stats),flush=True)


if __name__ == '__main__':
    main()
