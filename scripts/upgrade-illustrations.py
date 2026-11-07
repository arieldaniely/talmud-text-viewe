"""Upgrade bundled illustrations when a matching, licensed Wikisource file is larger."""

import argparse
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import gzip
import hashlib
import importlib
from io import BytesIO
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, NavigableString, Tag
import numpy as np
from PIL import Image, ImageOps

illustrations = importlib.import_module('import-illustrations')
ROOT = illustrations.ROOT
CACHE = illustrations.gemara.CACHE/'wikimedia'
WIKISOURCE = 'https://he.wikisource.org'
HOSTS = {'he.wikisource.org', 'commons.wikimedia.org', 'upload.wikimedia.org', 'thumb.wikimedia.org'}
AGENT = 'DafOpenIllustrationImporter/1.0 (offline Talmud viewer; public Wikisource assets)'
REVIEWED = json.loads((ROOT/'qa/wikisource-illustration-crops.json').read_text(encoding='utf8'))


def download(url):
    parts = urlsplit(url)
    if parts.scheme!='https' or parts.hostname not in HOSTS:
        raise ValueError(f'Unsupported Wikimedia URL: {url}')
    path = CACHE/hashlib.sha256(url.encode()).hexdigest()
    if path.exists():
        return path.read_bytes()
    with urlopen(Request(url,headers={'User-Agent':AGENT}),timeout=22) as response:
        content = response.read(8_000_001)
    if len(content)>8_000_000:
        raise ValueError('Wikimedia resource exceeds size limit')
    with NamedTemporaryFile(dir=CACHE,delete=False) as temp:
        temp.write(content)
    Path(temp.name).replace(path)
    return content


def daf_title(title, daf):
    numeral = ''
    number = int(daf[:-1])
    for letter,value in reversed(illustrations.gemara.HEBREW_NUMERALS.items()):
        count,number = divmod(number,value)
        numeral += letter*count
    numeral = numeral.replace('יה','טו').replace('יו','טז')
    return f'{title}_{numeral}_{"אב"[daf[-1]=="b"]}'


def original_url(url, srcset):
    parts = urlsplit('https:'+url if url.startswith('//') else url)
    path = parts.path
    if '/thumb/' in path:
        root,thumb = path.split('/thumb/',1)
        name = thumb.rsplit('/',1)[0]
        if name.lower().endswith(('.png','.jpg','.jpeg')):
            return urlunsplit(('https','upload.wikimedia.org',f'{root}/{name}','','')),unquote(name.rsplit('/',1)[-1])
        # Use Wikimedia's own raster rendition for vector images.
        choices = [choice.strip().split()[0] for choice in srcset.split(',') if choice.strip()]
        if choices:
            parts = urlsplit('https:'+choices[-1] if choices[-1].startswith('//') else choices[-1])
        return urlunsplit(('https',parts.netloc,parts.path,'','')),unquote(name.rsplit('/',1)[-1])
    return urlunsplit(('https',parts.netloc,path,'','')),unquote(path.rsplit('/',1)[-1])


@dataclass
class Artwork:
    file: str
    url: str
    thumbnail: str
    page: str
    before: str
    after: str
    section: str


def page_artwork(title):
    page = WIKISOURCE+'/wiki/'+quote(title)
    doc = BeautifulSoup(download(page),'html.parser')
    body = doc.select_one('.mw-parser-output')
    if body is None:
        return []
    text = []
    positions = []
    section = 'gemara'
    size = 0
    for node in body.descendants:
        if isinstance(node,Tag):
            if node.name in ('h2','h3'):
                heading = illustrations.commentary.key(node.get('id') or node.get_text()).removesuffix('עריכה')
                if heading in ('רשי','ראשי'):
                    section = 'rashi'
                elif heading in ('תוספות','תוס'):
                    section = 'tosafot'
                elif heading == 'גמרא':
                    section = 'gemara'
                else:
                    section = 'other'
            if node.name=='img':
                src = node.get('src','')
                href = node.find_parent('a')
                width = node.get('width','')
                if href and '/wiki/' in href.get('href','') and 'wikimedia.org/' in src and (not width.isdigit() or int(width)>20):
                    srcset = node.get('srcset','')
                    url,file = original_url(src,srcset)
                    if file.lower().endswith(('.png','.jpg','.jpeg','.svg')):
                        entries = [choice.strip().split()[0] for choice in srcset.split(',') if choice.strip()]
                        alternative = entries[-1] if entries else src
                        parts = urlsplit('https:'+alternative if alternative.startswith('//') else alternative)
                        thumbnail = urlunsplit(('https',parts.netloc,parts.path,'',''))
                        positions.append((size,file,url,thumbnail,section))
        elif isinstance(node,NavigableString):
            chunk = str(node)
            text.append(chunk)
            size += len(chunk)
    plain = ''.join(text)
    return [Artwork(file,url,thumbnail,page,plain[max(0,pos-300):pos],plain[pos:pos+300],section)
            for pos,file,url,thumbnail,section in positions]


def signature(image):
    rgba = image.convert('RGBA')
    paper = Image.new('RGBA',rgba.size,'white')
    paper.alpha_composite(rgba)
    ink = ImageOps.invert(paper.convert('L'))
    bounds = ink.point(lambda pixel:255 if pixel>=52 else 0).getbbox()
    if bounds is None:
        return None
    crop = ink.crop(bounds)
    pixels = np.asarray(crop.resize((48,48),Image.Resampling.LANCZOS),dtype=np.float32)
    return pixels/np.linalg.norm(pixels),crop.size


def shape_score(a,b):
    if a is None or b is None:
        return 0
    left,(w,h) = a
    right,(x,y) = b
    ratio = (w/h)/(x/y)
    if not .73<=ratio<=1.37:
        return 0
    return max(float(np.sum(left*np.roll(right,(i,j),(0,1)))) for i in (-1,0,1) for j in (-1,0,1))


def attribution(art):
    domain = 'commons.wikimedia.org' if '/commons/' in art.url else 'he.wikisource.org'
    params = {'action':'query','format':'json','titles':'File:'+art.file,'prop':'imageinfo',
              'iiprop':'extmetadata','iiextmetadatafilter':'Artist|LicenseShortName|LicenseUrl|UsageTerms'}
    value = json.loads(download('https://'+domain+'/w/api.php?'+urlencode(params)))
    page = next(iter(value.get('query',{}).get('pages',{}).values()),{})
    infos = page.get('imageinfo',[])
    if not infos:
        return None
    meta = infos[0]['extmetadata']
    license = meta.get('LicenseShortName',{}).get('value','')
    if not license.startswith(('CC BY','CC0','Public domain','PD')):
        return None
    if 'NC' in license or 'ND' in license:
        return None
    author = BeautifulSoup(meta.get('Artist',{}).get('value',''),'html.parser').get_text(' ',strip=True)
    return {'page':art.page,'file':'https://'+domain+'/wiki/File:'+quote(art.file.replace(' ','_')),
            'author':author,'license':license,'licenseUrl':meta.get('LicenseUrl',{}).get('value','')}


def source_section(ref):
    if ref.startswith('Rashi on '):
        return 'rashi'
    if ref.startswith('Tosafot on '):
        return 'tosafot'
    return 'gemara'


def upgrade_page(title, assets):
    try:
        artwork = page_artwork(title)
    except Exception as error:
        return [],[(title,str(error))]
    reviewed_entries = [REVIEWED.get(f"{asset['ref']}#{asset['index']}") for asset in assets]
    if all(reviewed_entries):
        selected = {entry[0] for entry in reviewed_entries}
        artwork = [art for art in artwork if art.file in selected]
    fixed_crops = {entry[0] for entry in reviewed_entries if entry and entry[1]}
    files = {}
    errors = []
    for art in artwork:
        if art.url in files:
            continue
        try:
            try:
                blob = download(art.url)
            except Exception:
                if art.thumbnail==art.url or art.file in fixed_crops:
                    raise
                blob = download(art.thumbnail)
            with Image.open(BytesIO(blob)) as raster:
                if raster.format not in ('PNG','JPEG'):
                    continue
                files[art.url] = blob,raster.format.lower(),raster.size,signature(raster)
        except Exception as error:
            errors.append((art.file,str(error)))
    chosen = []
    for asset in assets:
        reviewed = REVIEWED.get(f"{asset['ref']}#{asset['index']}")
        _,payload = asset['src'].split(',',1)
        with Image.open(BytesIO(base64.b64decode(payload))) as raster:
            base = signature(raster)
        if base is None:
            continue
        w,h = base[1]
        context = illustrations.commentary.key(asset.get('context',''))
        possible = {}
        for art in artwork:
            if art.url not in files:
                continue
            if reviewed and art.file==reviewed[0]:
                possible = {art.url: (2,art,None)}
                break
            _,_,_,candidate = files[art.url]
            if candidate is None:
                continue
            x,y = candidate[1]
            if min(x/w,y/h)<1.23:
                continue
            score = shape_score(base,candidate)
            nearby = illustrations.commentary.key(art.before+art.after)
            same_context = len(context)>=20 and context[-20:] in nearby
            same_section = source_section(asset['ref'])==art.section
            if score>=.87 or (score>=.78 and same_context and same_section):
                value = score+.08*same_context+.025*same_section
                previous = possible.get(art.url)
                if previous is None or value>previous[0]:
                    possible[art.url] = value,art,score
        ranked = sorted(possible.values(),key=lambda item:item[0],reverse=True)
        if not ranked or (len(ranked)>1 and ranked[0][0]-ranked[1][0]<.045):
            continue
        _,art,score = ranked[0]
        try:
            credit = attribution(art)
        except Exception as error:
            errors.append((art.file,str(error)))
            continue
        if credit is None:
            errors.append((art.file,'No supported license metadata'))
            continue
        blob,format,size,_ = files[art.url]
        upgraded = {'src':f'data:image/{format};base64,'+base64.b64encode(blob).decode('ascii'),
                    'size':list(size),'fileSource':credit,
                    'originalSha256':hashlib.sha256(asset['src'].encode()).hexdigest()}
        crop = reviewed[1] if reviewed and score is None else None
        if crop:
            upgraded.update(crop=crop,fileSize=list(size),size=crop[2:])
            if len(reviewed)>2:
                upgraded['clip'] = reviewed[2]
        chosen.append((asset,upgraded,'reviewed' if score is None else round(score,3)))
    return chosen,errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--book',type=int,action='append')
    args = parser.parse_args()
    CACHE.mkdir(parents=True,exist_ok=True)
    catalog_path = ROOT/'catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf8'))
    books = {}
    groups = defaultdict(list)
    owners = {}
    for book in catalog:
        if args.book and book['id'] not in args.book:
            continue
        path = ROOT/book['path']
        pages = json.loads(gzip.decompress(path.read_bytes()))
        books[book['id']] = book,path
        for page in pages:
            for asset in illustrations.page_images(page):
                if asset.get('fileSource'):
                    continue
                ref = asset['ref'].rsplit(' ',1)[1]
                daf = ref.split(':',1)[0]
                groups[daf_title(book['title'],daf)].append(asset)
                owners[id(asset)] = book['id']
    updates = defaultdict(dict)
    count = 0
    errors = []
    with ThreadPoolExecutor(8) as pool:
        jobs = {pool.submit(upgrade_page,title,assets):title for title,assets in groups.items()}
        for job in as_completed(jobs):
            matches,issues = job.result()
            errors.extend(issues)
            for asset,upgrade,score in matches:
                print(asset['ref'],asset['index'],asset['size'],upgrade['size'],score,upgrade['fileSource']['file'],flush=True)
                count += 1
                updates[owners[id(asset)]][illustrations.asset_key(asset)] = upgrade
    if args.apply:
        for ident in sorted(updates):
            book,path = books[ident]
            pages = json.loads(gzip.decompress(path.read_bytes()))
            for page in pages:
                for asset in illustrations.page_images(page):
                    asset.update(updates[ident].get(illustrations.asset_key(asset),{}))
            packed = gzip.compress(json.dumps(pages,ensure_ascii=False,separators=(',',':')).encode(),
                                   compresslevel=9,mtime=0)
            temp = path.with_suffix('.tmp')
            temp.write_bytes(packed)
            temp.replace(path)
            book.update(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())
        catalog_path.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (CACHE/'last-errors.json').write_text(json.dumps(errors,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(f'{count} verified upgrades across {len(groups)} Wikisource pages; {len(errors)} skipped fetches or files',flush=True)


if __name__=='__main__':
    main()
