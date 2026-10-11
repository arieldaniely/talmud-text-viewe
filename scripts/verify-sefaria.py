"""Corpus integrity plus representative browser checks after the local import."""
import gzip, hashlib, json, re
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from threading import Thread
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
catalog=json.loads((ROOT/'catalog.json').read_text(encoding='utf8'))
report={'pages':0,'convertedPages':0,'preservedCommentaries':0,'browserPages':[], 'browserErrors':[]}
totals={'streams':0,'positionedRanges':0,'characters':0,'figurePositions':0,'positionedHeadings':0,'sourceClassificationRepairs':0}
samples=[]
source_texts={}
for file in (ROOT/'.sefaria-cache').glob('*.json'):
    value=json.loads(file.read_text(encoding='utf-8-sig'))
    if isinstance(value,dict) and value.get('heVersionTitle')=='William Davidson Edition - Aramaic' and not value.get('sections'):
        source_texts[value['book']]=value['he']
def source_plain(value):
    import html
    value=re.sub(r'\s+',' ',html.unescape(re.sub('<[^>]+>','',value))).strip()
    return re.sub(r'[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]','',value).replace('\u05c3',':')
for book in catalog:
    packed=(ROOT/book['path']).read_bytes()
    assert len(packed)==book['bytes'] and hashlib.sha256(packed).hexdigest()==book['sha256']
    pages=json.loads(gzip.decompress(packed))
    original=json.loads(gzip.decompress((ROOT/'.sefaria-cache/original'/Path(book['path']).name).read_bytes()))
    assert len(pages)==len(original)==book['pages']
    scales=[]
    for page,old in zip(pages,original):
        report['pages']+=1
        totals['streams']+=len(page['streams']);totals['figurePositions']+=len(page['figures'])
        totals['sourceClassificationRepairs']+=bool(page.get('sourceRepairs'))
        totals['positionedHeadings']+=any(s['role']=='heading' for s in page['streams'])
        totals['characters']+=sum(len(s['text']) for s in page['streams'])
        totals['positionedRanges']+=sum(len(s['runs']) for s in page['streams'])
        assert page['n']==old['n'] and page['daf']==old['daf']
        assert not any(s['id'] in ('note-מסורתהשס','note-עיןמשפט') for s in page['streams'])
        for stream in page['streams']:
            if stream['role']=='commentary':
                before=next(s for s in old['streams'] if s['id']==stream.get('originStream',stream['id']))
                repair=next((r for r in page.get('sourceRepairs',[]) if r['stream']==stream['id']),None)
                if repair:
                    removed=set()
                    for start,length in repair['removedRanges']: removed.update(range(start,start+length))
                    assert stream['text']==''.join(c for i,c in enumerate(before['text']) if i not in removed)
                    assert [r[2:] for r in stream['runs']]==[r[2:] for r in before['runs'] if not any(i in removed for i in range(r[0],r[0]+r[1]))]
                elif stream.get('originStream'):
                    assert stream['text']==before['text'] and stream['runs']==before['runs']
                else: assert stream==before
                report['preservedCommentaries']+=1
            if stream.get('source',{}).get('provider')!='Sefaria': continue
            report['convertedPages']+=1
            assert stream['source']['versionTitle']=='William Davidson Edition - Aramaic'
            title,address=stream['source']['ref'].rsplit(' ',1)
            index=int(address[:-1])*2-2+(address[-1]=='b')
            assert stream['text']==source_plain(' '.join(source_plain(v) for v in source_texts[title][index]))
            assert 'references' not in stream
            words=list(re.finditer(r'\S+',stream['text']))
            assert [(r[0],r[1]) for r in stream['runs']]==[(m.start(),len(m.group())) for m in words]
            assert all(0<=r[2]<r[3]<=page['width'] and 0<r[4]<page['height'] for r in stream['runs'])
            assert not re.search(r'<[^>]+>|[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]',stream['text'])
            row_sizes={r[2]:r[3] for r in stream['sourceRows']}
            assert all(r[5]<=row_sizes[r[4]]+.01 for r in stream['runs']),('Tall word on ordinary baseline',book['id'],page['n'])
            scales.append((min(r[5] for r in stream['runs']),page['n']))
    samples.extend((book['id'],n) for n in sorted({1,2,len(pages),min(scales)[1] if scales else 1}))
samples=sorted(set(samples+[(5,31),(6,55),(11,154),(13,47),(13,59),(30,281)]))
class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT)))
Thread(target=server.serve_forever,daemon=True).start()
base=f'http://127.0.0.1:{server.server_port}'
(ROOT/'qa').mkdir(exist_ok=True)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    tab=browser.new_page(viewport={'width':1400,'height':1200})
    tab.route('https://fonts.googleapis.com/**',lambda route:route.abort())
    tab.on('pageerror',lambda e:report['browserErrors'].append(str(e)))
    for book,n in samples:
        tab.goto(f'{base}/#book={book}&page={n}')
        tab.wait_for_function(f'window.liteViewerState?.book==={book} && window.liteViewerState?.page==={n}')
        tab.evaluate('document.fonts.ready')
        result=tab.evaluate('''()=>{
          const svg=document.querySelector('.page-facsimile');
          const spans=[...svg.querySelectorAll('.gemara tspan')];
          return {words:spans.length,clipped:spans.filter(s=>{
            const b=s.getBBox();return b.x<-.5||b.x+b.width>current.width+.5||b.y<0||b.y+b.height>current.height+.5;
          }).length,notes:svg.querySelectorAll('.reference-note').length,
          badFonts:[...document.fonts].filter(f=>f.status==='error').map(f=>f.family)};
        }''')
        assert result['notes']==0 and result['clipped']==0 and not result['badFonts'],(book,n,result)
        report['browserPages'].append({'book':book,'page':n,**result})
        if (book,n) in ((0,1),(12,2),(25,1),(5,1),(5,31),(6,55),(11,154),(13,47),(13,59),(30,281)):
            tab.locator('.page-facsimile').screenshot(path=str(ROOT/f'qa/sefaria-{book}-{n}.png'))
    tab.goto(f'{base}/#book=0&page=1')
    tab.wait_for_function('window.liteViewerState?.book===0 && window.liteViewerState?.page===1')
    tab.locator('#query').fill('מאימתי קורין');tab.locator('#searchForm button').click()
    assert tab.locator('.gemara .match').count()>=2
    tab.evaluate("Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.copiedText=value}}})")
    tab.locator('#copy').click();tab.wait_for_function('window.copiedText')
    copied=tab.evaluate('window.copiedText')
    assert 'מאימתי קורין' in copied and '\nמסורת הש״ס\n' not in copied and '\nעין משפט · נר מצוה\n' not in copied
    tab.locator('#rashiFont').uncheck();assert not tab.evaluate("document.body.classList.contains('use-rashi')")
    tab.locator('#rashiFont').check();tab.locator('#theme').click();assert tab.evaluate("document.body.classList.contains('dark')")
    tab.locator('#larger').click();assert tab.locator('.page-facsimile').is_visible()
    tab.locator('#notes').uncheck();assert tab.locator('.reference-note').count()==0
    tab.set_viewport_size({'width':390,'height':844})
    assert tab.locator('.page-facsimile').evaluate('(s)=>parseFloat(getComputedStyle(s).width)')<400
    browser.close()
server.shutdown();server.server_close()
assert not report['browserErrors']
(ROOT/'qa/sefaria-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
quality=json.loads((ROOT/'quality.json').read_text(encoding='utf8'))
quality.update(books=len(catalog),pages=report['pages'],**{k:v for k,v in totals.items() if k not in ('positionedHeadings','sourceClassificationRepairs')},dataBytes=sum(b['bytes'] for b in catalog),validationPassed=True,checkedBrowserPages=len(report['browserPages']),browserCoordinateErrors=sum(s['clipped'] for s in report['browserPages']),browserScriptErrors=report['browserErrors'],layout='Sefaria Gemara fitted to source row regions; commentary retains source positions',sefariaConvertedPages=report['convertedPages'],sourceClassificationRepairs=totals['sourceClassificationRepairs'])
quality['headingsAndReferences']={'pages':report['pages'],'positionedHeadings':totals['positionedHeadings'],'embeddedHeadings':0,'referenceColumns':0,'failures':[]}
quality['packageBytes']=sum(f.stat().st_size for f in ROOT.rglob('*') if f.is_file() and not any(v in ('.git','.sefaria-cache','__pycache__') for v in f.relative_to(ROOT).parts) and f.suffix!='.png')
(ROOT/'quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps({**report,'browserPages':len(report['browserPages'])},ensure_ascii=False))
