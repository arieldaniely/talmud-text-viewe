"""Download explicit editions, then replace local Gemara without browser API calls.

Run from any directory. Cache and backups are ignored by Git. --apply is required
to write the corpus. Missing versions/pages fail instead of using the default.
"""
import argparse, collections, difflib, gzip, hashlib, html, json, re, time
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.sefaria-cache'
EDITION = 'William Davidson Edition - Aramaic'
TITLES = ['Berakhot','Niddah','Beitzah','Chagigah','Yoma','Megillah',
          'Moed Katan','Sukkah','Eruvin','Pesachim','Rosh Hashanah','Shabbat',
          'Jerusalem Talmud Shekalim','Taanit','Bava Batra','Bava Metzia',
          'Bava Kamma','Horayot','Makkot','Sanhedrin','Avodah Zarah','Shevuot',
          'Gittin','Yevamot','Ketubot','Nedarim','Nazir','Sotah','Kiddushin',
          'Zevachim','Chullin','Keritot','Menachot','Meilah','Arakhin','Temurah']
HIDDEN = {'note-מסורתהשס', 'note-עיןמשפט'}
# Audited source classification errors: principal-text font runs had been
# placed in a commentary stream. These repairs do not import commentary text.
REPAIRS={(5,31):('tosafot',12.96),(6,55):('tosafot',13.68),
         (13,47):('tosafot',13.68),(13,59):('tosafot',13.68),
         (30,281):('rashi',13.68),(11,154):('rashi',13.68)}

def repair_geometry(page, book_id, gemara):
    if page.get('sourceRepairs'): return gemara
    repair=REPAIRS.get((book_id,page['n']))
    if not repair: return gemara
    ident,size=repair
    source=next(s for s in page['streams'] if s['id']==ident)
    if (book_id,page['n'])==(11,154):
        # This entire old 'gemara' stream is printed Tosafot, while the
        # actual central text is mixed into Rashi.
        continuation={**gemara,'id':'tosafot-continuation','role':'commentary','label':'תוספות — המשך','originStream':'gemara'}
        page['streams'].remove(gemara);page['streams'].append(continuation)
        gemara={'id':'gemara','label':'גמרא','role':'gemara','text':'','runs':[]}
        page['streams'].insert(0,gemara)
        chosen=[r for r in source['runs'] if abs(r[5]-size)<.02]
    else:
        body=[r for r in gemara['runs'] if abs(r[5]-size)<.02]
        left=min(r[2] for r in body);right=max(r[3] for r in body);top=min(r[4] for r in body)
        chosen=[r for r in source['runs'] if abs(r[5]-size)<.02 and r[4]<top and r[2]>=left-.5 and r[3]<=right+.5]
    if len(chosen)<20: raise ValueError('Audited geometry repair no longer matches')
    rows=rows_of({'runs':gemara['runs']+chosen})
    principal_rows=rows_of({'runs':chosen})
    references=[r for r in source['runs'] if r[6] and any(abs(r[4]-row[2])<size*.6 and r[2]>=row[0]-1 and r[3]<=row[1]+1 for row in principal_rows)]
    removed=chosen+references; indices=set()
    for r in removed: indices.update(range(r[0],r[0]+r[1]))
    offsets=[0]
    for i in range(len(source['text'])): offsets.append(offsets[-1]+(i not in indices))
    source['text']=''.join(c for i,c in enumerate(source['text']) if i not in indices)
    source['runs']=[[offsets[r[0]],r[1],*r[2:]] for r in source['runs'] if r not in removed]
    for key in ('references','literalLatin'): source.pop(key,None)
    page['sourceRepairs']=[{'stream':ident,'removedRanges':[[r[0],r[1]] for r in removed],'reason':'Principal Gemara font runs classified as commentary in original extraction'}]
    gemara['sourceRows']=rows
    return gemara

def fetch(path, params=None):
    url = 'https://www.sefaria.org/api/' + quote(path, safe='/')
    if params: url += '?' + urlencode(params)
    key = hashlib.sha256(url.encode()).hexdigest()
    file = CACHE / (key + '.json')
    if file.exists(): return json.loads(file.read_text(encoding='utf8'))
    CACHE.mkdir(exist_ok=True)
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={'User-Agent':'LocalTalmudImporter/1.0'}), timeout=120) as response:
                value = json.load(response)
            if isinstance(value, dict) and value.get('error'): raise ValueError(value['error'])
            file.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
            return value
        except Exception:
            if attempt == 3: raise
            time.sleep(2 ** attempt)

def plain(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub('<[^>]+>', '', value))).strip()

def tokens(value):
    return [re.sub(r'[^א-ת]', '', m.group()) for m in re.finditer(r'\S+', value)]

def rows_of(stream):
    groups = collections.defaultdict(list)
    for run in stream['runs']:
        if not run[6]: groups[round(run[4], 1)].append(run)
    rows = []
    for y, runs in sorted(groups.items()):
        # Keep the source line envelope, including its expanded lower column.
        rows.append([min(r[2] for r in runs), max(r[3] for r in runs), y,
                     collections.Counter(round(r[5], 2) for r in runs).most_common(1)[0][0]])
    return normalize_rows(rows)

def normalize_rows(rows):
    """Mixed fonts can differ by a fraction of a point on the same baseline.

    Those are parts of one physical line, not two lines that may each be filled.
    The widest part supplies its body size and baseline.
    """
    groups=[]
    for row in sorted(rows,key=lambda r:r[2]):
        if groups and row[2]-groups[-1][0][2]<=1.2: groups[-1].append(row)
        else: groups.append([row])
    result=[]
    for group in groups:
        main=max(group,key=lambda r:r[1]-r[0])
        result.append([min(r[0] for r in group),max(r[1] for r in group),main[2],main[3]])
    return result

def reflow(stream, text, advance):
    rows = normalize_rows(stream.get('sourceRows') or rows_of(stream))
    if not rows: raise ValueError('No Gemara geometry')
    words = list(re.finditer(r'\S+', text))
    # Preserve large opening words only where they survive in the new text.
    big = {}
    old_words = list(re.finditer(r'\S+', stream['text']))
    old_sizes = {r[0]:r[5] for r in stream['runs'] if r[5] > 18 and not r[6]}
    if stream.get('sourceRows'):
        # Preserve the original large-word size when rerunning from a checkout
        # that already contains Sefaria data but has no PDF-text backup.
        for r in stream['runs']:
            original=next((row[3] for row in rows if abs(row[2]-r[4])<.05 and row[3]>18),None)
            if original and r[0] in old_sizes: old_sizes[r[0]]=original
    blocks=difflib.SequenceMatcher(None, tokens(stream['text']), tokens(text), autojunk=False).get_matching_blocks() if old_sizes else []
    for block in blocks:
        for i in range(block.size):
            size = old_sizes.get(old_words[block.a+i].start())
            if size: big[block.b+i] = size
    def fit(scale):
        result = []; index = 0
        for left, right, y, size in rows:
            line = []; used = 0; gap = size * scale * .22
            while index < len(words):
                # A displaced chapter opener must not acquire a tall font on
                # an ordinary baseline, where it would overlap adjacent lines.
                m = words[index]; fs = min(big.get(index,size),size) * scale
                if size > 18 and index not in big: break
                width = advance(m.group(), fs)
                if used + (gap if line else 0) + width > right-left + .01: break
                used += (gap if line else 0) + width
                line.append((m, width, fs)); index += 1
            if not line: continue
            spacing = (right-left-sum(w for _,w,_ in line))/(len(line)-1) if len(line)>1 and index<len(words) else gap
            x = right
            for m, width, fs in line:
                result.append([m.start(),len(m.group()),round(x-width,3),round(x,3),y,round(fs,3),0])
                x -= width + spacing
        return result, index
    result, count = fit(1)
    scale = 1
    if count < len(words):
        lo, hi = .45, 1
        if fit(lo)[1] < len(words): raise ValueError('Gemara cannot fit source region')
        for _ in range(14):
            mid = (lo+hi)/2
            if fit(mid)[1] == len(words): lo = mid
            else: hi = mid
        scale = lo; result, count = fit(scale)
    assert count == len(words)
    return result, rows, round(scale,4)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--book', type=int, action='append')
    args = parser.parse_args()
    # Font advances use the actual bundled font, not character-count estimates.
    from fontTools.ttLib import TTFont
    font = TTFont(ROOT/'fonts/romm-vilna.ttf'); cmap = font.getBestCmap(); units = font['head'].unitsPerEm
    @lru_cache(maxsize=50000)
    def width(word):
        return sum(font['hmtx'][cmap.get(c,'.notdef')][0] for c in word)/units
    def advance(word, size): return width(word)*size
    catalog = json.loads((ROOT/'catalog.json').read_text(encoding='utf8'))
    report = {'edition':EDITION,'books':[], 'exceptions':[]}
    changes = []
    for book in catalog:
        if args.book and book['id'] not in args.book: continue
        title = TITLES[book['id']]
        original=CACHE/'original'/Path(book['path']).name
        pages = json.loads(gzip.decompress((original if original.exists() else ROOT/book['path']).read_bytes()))
        edition = EDITION
        if book['id']==12:
            response=None;metadata={}
            report['exceptions'].append({'book':title,'reason':'Davidson Hebrew omits printed passages; alternative editions require reviewed page alignment. Original Gemara retained.'})
        else:
            versions = fetch('texts/versions/'+title)
            metadata = next((v for v in versions if v['language']=='he' and v['versionTitle']==edition), None)
            if not metadata: raise ValueError('Required edition missing: '+title)
            response = fetch('texts/'+title, {'pad':0,'commentary':0,'context':0,'vhe':edition,'fallbackOnDefaultVersion':0,'stripItags':1})
            if response.get('heVersionTitle') != edition: raise ValueError('Unexpected edition: '+title)
        stats = {'title':title,'pages':0,'scales':[]}
        for page in pages:
            page['streams'] = [s for s in page['streams'] if s['id'] not in HIDDEN and s.get('parent') not in HIDDEN]
            if book['id']==12: continue
            if book['id']==33 and (42<=page['n']<=47 or page['n']>=65):
                report['exceptions'].append({'book':title,'page':page['n'],'reason':'Kinnim/Middot are Mishnah, not Gemara; original text retained'})
                continue
            gemara = next((s for s in page['streams'] if s['id']=='gemara'), None)
            if not gemara: raise ValueError(f'Missing Gemara {title} {page["n"]}')
            gemara=repair_geometry(page,book['id'],gemara)
            if not any(s['role']=='heading' for s in page['streams']):
                heading=[r for r in gemara['runs'] if r[0]<len(page['title'])]
                if not heading: raise ValueError('Missing embedded page heading')
                end=max(r[0]+r[1] for r in heading)
                page['streams'].append({'id':'header','label':'כותרת הדף','role':'heading','text':gemara['text'][:end],'runs':heading})
                gemara['runs']=[r for r in gemara['runs'] if r not in heading]
            numeral, side = page['daf'].split(' ע״')
            number = sum({'א':1,'ב':2,'ג':3,'ד':4,'ה':5,'ו':6,'ז':7,'ח':8,'ט':9,'י':10,'כ':20,'ל':30,'מ':40,'נ':50,'ס':60,'ע':70,'פ':80,'צ':90,'ק':100,'ר':200,'ש':300,'ת':400}[c] for c in numeral)
            address = number*2-2+(side=='ב')
            page_title=title
            page_response=response
            if book['id']==33 and 48<=page['n']<=64:
                page_title='Tamid'
                page_response=fetch('texts/Tamid', {'pad':0,'commentary':0,'context':0,'vhe':EDITION,'fallbackOnDefaultVersion':0,'stripItags':1})
                if page_response.get('heVersionTitle')!=EDITION: raise ValueError('Unexpected Tamid edition')
                metadata=next(v for v in fetch('texts/versions/Tamid') if v['language']=='he' and v['versionTitle']==EDITION)
            segments = page_response['he'][address]
            if not segments or not any(plain(v) for v in segments):
                report['exceptions'].append({'book':title,'page':page['n'],'daf':page['daf'],'reason':'No Gemara in requested edition; original stream retained (Nazir 33b contains printed Tosafot)'})
                continue
            if not all(isinstance(v,str) for v in segments): raise ValueError('Invalid segment structure')
            text = ' '.join(plain(v) for v in segments)
            text = re.sub(r'[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]', '', text).replace('\u05c3', ':')
            runs, rows, scale = reflow(gemara, text, advance)
            gemara.update(text=text,runs=runs,sourceRows=rows,source={'provider':'Sefaria','ref':f'{page_title} {number}{"a" if side=="א" else "b"}', 'versionTitle':edition,'license':metadata.get('license'),'versionSource':metadata.get('versionSource')})
            for key in ('references','literalLatin'): gemara.pop(key,None)
            # Offsets attached to replaced text are invalid; retained commentaries
            # and their geometry remain unchanged.
            page['streams'] = [s for s in page['streams'] if s.get('parent')!='gemara']
            page['figures'] = [f for f in page['figures'] if f['stream']!='gemara']
            stats['pages'] += 1; stats['scales'].append([page['n'],scale])
        packed = gzip.compress(json.dumps(pages,ensure_ascii=False,separators=(',',':')).encode(),compresslevel=9,mtime=0)
        changes.append((book,packed))
        report['books'].append(stats)
        print(title,stats['pages'],'minimum scale',min((s[1] for s in stats['scales']),default=1),flush=True)
    if args.apply:
        backup = CACHE/'original';backup.mkdir(exist_ok=True)
        for book,packed in changes:
            path = ROOT/book['path']; saved = backup/path.name
            if not saved.exists(): saved.write_bytes(path.read_bytes())
            path.write_bytes(packed);book.update(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())
            data=json.loads(gzip.decompress(packed));book['characters']=sum(len(s['text']) for p in data for s in p['streams'])
        (ROOT/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        report_path=ROOT/'sefaria-import.json'
        if args.book and report_path.exists():
            previous=json.loads(report_path.read_text(encoding='utf8')); updated={TITLES[b] for b in args.book}
            report['books']=[b for b in previous['books'] if b['title'] not in updated]+report['books']
            report['exceptions']=[e for e in previous['exceptions'] if e['book'] not in updated]+report['exceptions']
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    else:
        (CACHE/'preview.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')

if __name__=='__main__': main()
