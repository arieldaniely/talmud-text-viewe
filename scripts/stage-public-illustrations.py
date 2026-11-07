"""Keep unverified picture files in the ignored local viewer only."""

from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

from public_content import licensed_illustration

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT/'.sefaria-cache/local-viewer'


def write(path, content):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_bytes(content)
    temp.replace(path)


def summarize(pages):
    result = Counter()
    for page in pages:
        in_slots = [image for slot in page['figures'] for image in slot.get('images',[])]
        outside = page.get('illustrations',[])
        inline = len(in_slots)+sum('box' in image for image in outside)
        result.update(images=len(in_slots)+len(outside),inline=inline,
                      belowPage=len(in_slots)+len(outside)-inline,
                      sharedSlots=sum(len(slot.get('images',[]))>1 for slot in page['figures']),
                      upgraded=sum(licensed_illustration(image) for image in (*in_slots,*outside)))
    return result


def main():
    catalog_path = ROOT/'catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf8'))
    local_catalog = []
    all_local = Counter()
    all_public = Counter()
    for book in catalog:
        path = ROOT/book['path']
        packed = path.read_bytes()
        pages = json.loads(gzip.decompress(packed))
        full = summarize(pages)
        local = LOCAL/book['path']
        local_entry = dict(book)
        if full['images']>full['upgraded']:
            write(local,packed)
            for page in pages:
                for slot in page['figures']:
                    if 'images' in slot:
                        approved = [image for image in slot['images'] if licensed_illustration(image)]
                        if approved:
                            slot['images'] = approved
                        else:
                            slot.pop('images')
                approved = [image for image in page.get('illustrations',[]) if licensed_illustration(image)]
                if approved:
                    page['illustrations'] = approved
                else:
                    page.pop('illustrations',None)
            filtered = gzip.compress(json.dumps(pages,ensure_ascii=False,separators=(',',':')).encode(),
                                     compresslevel=9,mtime=0)
            write(path,filtered)
            book.update(bytes=len(filtered),sha256=hashlib.sha256(filtered).hexdigest())
        elif local.exists():
            cached = local.read_bytes()
            full = summarize(json.loads(gzip.decompress(cached)))
            local_entry.update(bytes=len(cached),sha256=hashlib.sha256(cached).hexdigest())
        local_catalog.append(local_entry)
        all_local.update(full)
        all_public.update(summarize(pages))
    write(catalog_path,(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n').encode())
    write(LOCAL/'catalog.json',(json.dumps(local_catalog,ensure_ascii=False,indent=2)+'\n').encode())
    for name in ('quality.json','qa/sefaria-validation.json'):
        report_path = ROOT/name
        report = json.loads(report_path.read_text(encoding='utf8'))
        report['illustrations'] = dict(all_public)
        if name=='quality.json':
            report['dataBytes'] = sum(book['bytes'] for book in catalog)
            report['packageBytes'] = sum(path.stat().st_size for path in ROOT.rglob('*')
                if path.is_file() and path.suffix!='.png'
                and not set(path.relative_to(ROOT).parts)&{'.git','.sefaria-cache','__pycache__'})
        write(report_path,(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode())
    print(f'Local illustrations: {dict(all_local)}\nPublishable illustrations: {dict(all_public)}')


if __name__ == '__main__':
    main()
