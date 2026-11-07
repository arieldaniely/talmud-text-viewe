"""Corpus integrity plus representative browser checks after the local import."""
import gzip, hashlib, importlib, json, re
from collections import Counter, defaultdict
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from threading import Thread
from pathlib import Path
from playwright.sync_api import sync_playwright
from public_content import exclude_gra_notes, has_gra_heading, verify_illustrations

gemara_import = importlib.import_module('import-sefaria')
commentary_import = importlib.import_module('import-commentary')
used_sources = defaultdict(list)
commentary_totals = Counter()

def verify_commentary_geometry(before, after):
    def references(stream):
        text=stream['text']
        marked=[(text[r[0]:r[0]+r[1]],*r[2:]) for r in stream['runs'] if r[6]]
        offsets=[text[p:p+n] for p,n in stream.get('references',[])]
        return marked,offsets

    assert references(before)==references(after)
    groups=defaultdict(list)
    for run in before['runs']:
        groups[run[4]].append(run)
    rows={y:(min(r[2] for r in runs),max(r[3] for r in runs),max(r[5] for r in runs))
          for y,runs in groups.items()}
    for _,_,left,right,y,size,_ in after['runs']:
        start,end,original_size=rows[y]
        assert start-.002<=left<=right<=end+.002 and size<=original_size+.002

ROOT=Path(__file__).resolve().parents[1]
catalog=json.loads((ROOT/'catalog.json').read_text(encoding='utf8'))
report={'pages':0,'convertedPages':0,'preservedCommentaries':0,'browserPages':[], 'browserErrors':[]}
gemara_sections=Counter()
totals={'streams':0,'positionedRanges':0,'characters':0,'figurePositions':0,'positionedHeadings':0,'sourceClassificationRepairs':0}
samples=[]
illustration_samples={}
illustrations=set()
illustration_totals=Counter()
source_texts={}
commentary_texts={}
for file in (ROOT/'.sefaria-cache').glob('*.json'):
    value=json.loads(file.read_text(encoding='utf-8-sig'))
    if isinstance(value,dict) and value.get('heVersionTitle')=='William Davidson Edition - Aramaic' and not value.get('sections'):
        source_texts[value['book']]=value['he']
    if (isinstance(value,dict) and value.get('heVersionTitle') in ('Vilna Edition','WikiSource Rashi')
            and not value.get('sections') and value.get('book','').startswith(('Rashi on ', 'Tosafot on '))):
        comments, _ = commentary_import.source_tokens(value['he'],value['book'].split(' on ',1)[0])
        commentary_texts[value['book'],value['heVersionTitle']]={ref:text for ref,text,_ in comments}
for book in catalog:
    packed=(ROOT/book['path']).read_bytes()
    assert len(packed)==book['bytes'] and hashlib.sha256(packed).hexdigest()==book['sha256']
    pages=json.loads(gzip.decompress(packed))
    original_path=ROOT/'.sefaria-cache/original'/Path(book['path']).name
    original=json.loads(gzip.decompress(original_path.read_bytes())) if original_path.exists() else pages
    backup=commentary_import.BACKUP/Path(book['path']).name
    printed=json.loads(gzip.decompress(backup.read_bytes())) if backup.exists() else None
    assert len(pages)==len(original)==book['pages']
    scales=[]
    for page,old in zip(pages,original):
        assert exclude_gra_notes(page)==0
        image_count=verify_illustrations(page,illustrations)
        if image_count:
            supplementary=page.get('illustrations',[])
            placed=sum(len(slot.get('images',[])) for slot in page['figures'])
            inferred=sum('box' in image for image in supplementary)
            shared=sum(len(slot.get('images',[]))>1 for slot in page['figures'])
            all_images=[*supplementary,*(image for slot in page['figures'] for image in slot.get('images',[]))]
            upgraded=sum('fileSource' in image for image in all_images)
            illustration_totals.update(images=image_count, inline=placed+inferred,
                                       belowPage=image_count-placed-inferred, sharedSlots=shared, upgraded=upgraded)
            for case,value in (('original',placed),('inferred',inferred),('shared',shared),
                               ('belowPage',image_count-placed-inferred),('upgraded',upgraded)):
                if value:
                    illustration_samples.setdefault(case,(book['id'],page['n']))
        report['pages']+=1
        totals['streams']+=len(page['streams']);totals['figurePositions']+=len(page['figures'])
        totals['sourceClassificationRepairs']+=bool(page.get('sourceRepairs'))
        totals['positionedHeadings']+=any(s['role']=='heading' for s in page['streams'])
        totals['characters']+=sum(len(s['text']) for s in page['streams'])
        totals['positionedRanges']+=sum(len(s['runs']) for s in page['streams'])
        assert page['n']==old['n'] and page['daf']==old['daf']
        assert not any(s['id'] in ('note-מסורתהשס','note-עיןמשפט') for s in page['streams'])
        for stream in page['streams']:
            source=stream.get('source',{})
            if 'fragments' in source:
                assert stream['role']=='commentary' and source['provider']=='Sefaria'
                assert source['license'] in ('Public Domain','CC-BY-SA')
                author=source['ref'].split(' on ',1)[0]
                assert stream['id'] in commentary_import.AUTHORS[author]
                comments=commentary_texts.get((source['ref'],source['versionTitle']))
                previous_end=0
                for start,length,ref,offset in source['fragments']:
                    assert previous_end<=start<start+length<=len(stream['text']) and offset>=0
                    if comments is not None:
                        assert stream['text'][start:start+length]==comments[ref][offset:offset+length],(source['ref'],ref)
                        commentary_totals['verifiedFragments']+=1
                    used_sources[source['ref'],ref].append((offset,offset+length,page['n']))
                    previous_end=start+length
                assert all(0<=r[0]<r[0]+r[1]<=len(stream['text']) for r in stream['runs'])
                assert all(a[0]+a[1]<=b[0] for a,b in zip(stream['runs'],stream['runs'][1:]))
                headings=stream.get('headings',[])
                assert all(0<=start<start+length<=len(stream['text']) for start,length in headings)
                assert all(a+length<=b for (a,length),(b,_) in zip(headings,headings[1:]))
                commentary_totals['headingRanges']+=len(headings)
                if printed is not None:
                    before=next(s for s in printed[page['n']-1]['streams'] if s['id']==stream['id'])
                    verify_commentary_geometry(before,stream)
                commentary_totals['streams']+=1
                continue
            if stream['role']=='commentary':
                report['preservedCommentaries']+=1
            if stream['role']=='commentary' and original_path.exists():
                before=next(s for s in old['streams'] if s['id']==stream.get('originStream',stream['id']))
                repair=next((r for r in page.get('sourceRepairs',[]) if r['stream']==stream['id']),None)
                if has_gra_heading(before['text']):
                    def positioned_words(s):
                        return Counter((s['text'][start:start+length],*coords)
                                       for start,length,*coords in s['runs'])
                    assert not (positioned_words(stream)-positioned_words(before))
                elif repair:
                    removed=set()
                    for start,length in repair['removedRanges']: removed.update(range(start,start+length))
                    assert stream['text']==''.join(c for i,c in enumerate(before['text']) if i not in removed)
                    assert [r[2:] for r in stream['runs']]==[r[2:] for r in before['runs'] if not any(i in removed for i in range(r[0],r[0]+r[1]))]
                elif stream.get('originStream'):
                    assert stream['text']==before['text'] and stream['runs']==before['runs']
                else: assert stream==before
            if stream.get('source',{}).get('provider')!='Sefaria': continue
            report['convertedPages']+=1
            assert stream['source']['versionTitle']=='William Davidson Edition - Aramaic'
            title,address=stream['source']['ref'].rsplit(' ',1)
            index=int(address[:-1])*2-2+(address[-1]=='b')
            opening=gemara_import.starts_tractate(book['id'],page['n'])
            if opening:
                assert not gemara_import.OPENING_MISHNAH.match(stream['text'].lstrip())
            if title in source_texts:
                text,sections=gemara_import.gemara_text(source_texts[title][index],opening)
                assert stream['text']==text
                assert stream.get('sectionStarts',[])==sections
            assert 'references' not in stream
            words=list(re.finditer(r'\S+',stream['text']))
            assert [(r[0],r[1]) for r in stream['runs']]==[(m.start(),len(m.group())) for m in words]
            assert all(0<=r[2]<r[3]<=page['width'] and 0<r[4]<page['height'] for r in stream['runs'])
            assert not re.search(r'<[^>]+>|[\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7]',stream['text'])
            row_sizes={r[2]:r[3] for r in stream['sourceRows']}
            sections=set(stream.get('sectionStarts',[]))
            gemara_sections['sectionStarts']+=len(sections)
            assert all(r[5]<=row_sizes[r[4]]*(gemara_import.SECTION_SCALE if r[0] in sections else 1)+.01
                       for r in stream['runs']),('Tall word on ordinary baseline',book['id'],page['n'])
            for start,length,*_ in stream['runs']:
                if start in sections:
                    assert gemara_import.SECTION.fullmatch(stream['text'][start:start+length])
            if 'sourceOpening' in stream:
                gemara_sections['openingWords']+=1
                y=stream['sourceOpening']['box'][2]
                assert any(r[4]==y and r[5]>=gemara_import.TITLE_SIZE for r in stream['runs'])
            scales.append((min(r[5] for r in stream['runs']),page['n']))
    samples.extend((book['id'],n) for n in sorted({1,2,len(pages),min(scales)[1] if scales else 1}))
samples=sorted(set(samples+list(illustration_samples.values())+[(5,31),(6,55),(7,13),(11,154),(13,47),(13,59),(30,281)]))
for fragments in used_sources.values():
    fragments.sort()
    assert all(a[1]<=b[0] for a,b in zip(fragments,fragments[1:])),fragments
    commentary_totals['splitComments']+=len({f[2] for f in fragments})>1
print(f"Verified {report['pages']} pages and {commentary_totals['verifiedFragments']} Sefaria commentary fragments", flush=True)
class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT)))
Thread(target=server.serve_forever,daemon=True).start()
base=f'http://127.0.0.1:{server.server_port}'
(ROOT/'qa').mkdir(exist_ok=True)
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
    tab=browser.new_page(viewport={'width':1400,'height':1200})
    tab.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.copiedText=value}}})")
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
        if (book,n) in illustration_samples.values():
            images=tab.evaluate("""async () => {
              const previews=[...document.querySelectorAll('.illustration-cards img,.illustration-cards svg image')];
              const inline=[...document.querySelectorAll('.page-illustration')];
              await Promise.all(previews.map(async el => {
                const copy=new Image();copy.src=el.src??el.href.baseVal;await copy.decode();
              }));
              return {previews:previews.length, inline:inline.length,
                      expected:current.figures.flatMap(f=>f.images??[]).length+
                               (current.illustrations??[]).filter(f=>f.box).length};
            }""")
            assert images['previews'] and images['inline']==images['expected'],(book,n,images)
            if images['inline']:
                link=tab.locator('.illustration-link:has(.page-illustration)').first
                destination=link.get_attribute('href')[1:]
                link.locator('.illustration-hit').click()
                assert tab.evaluate('document.activeElement.id')==destination
                tab.locator(f'#{destination} button').click()
                assert tab.locator('.positioned-stream .flash').count()
            tab.evaluate('window.scrollTo(0,0)')
        if (book,n)==(7,13) and illustrations:
            assert tab.locator('.page-illustration[data-stream=gemara]').count()==2
            assert tab.evaluate("current.figures.some(slot => slot.stream==='tosafot' && slot.layout==='pdf' && slot.images?.length===3)")
            tab.locator('.page-facsimile').screenshot(path=str(ROOT/'qa/illustrations-sukkah-8a.png'))
        if (book,n) in ((0,1),(1,1),(12,2),(25,1),(5,1),(5,31),(6,55),(11,154),(13,47),(13,59),(30,281),(34,1)):
            tab.locator('.page-facsimile').screenshot(path=str(ROOT/f'qa/sefaria-{book}-{n}.png'))
    tab.goto(f'{base}/#book=0&page=1')
    tab.wait_for_function('window.liteViewerState?.book===0 && window.liteViewerState?.page===1')
    rashi=tab.locator('#stream-rashi .dibbur-hamatchil')
    tosafot=tab.locator('#stream-tosafot .dibbur-hamatchil')
    assert rashi.count()>4 and tosafot.count()>4
    assert any(word.endswith('.') for word in rashi.all_text_contents())
    assert tosafot.first.evaluate('(node)=>getComputedStyle(node).fontWeight')=='700'
    tab.locator('#query').fill('מאימתי קורין');tab.locator('#searchForm button').click()
    assert tab.locator('.gemara .match').count()>=2
    tab.locator('#copy').click();tab.wait_for_function('window.copiedText')
    copied=tab.evaluate('window.copiedText')
    assert 'מאימתי קורין' in copied and '\nמסורת הש״ס\n' not in copied and '\nעין משפט · נר מצוה\n' not in copied
    tab.goto(f'{base}/#book=1&page=1')
    tab.wait_for_function('window.liteViewerState?.book===1 && window.liteViewerState?.page===1')
    assert tab.locator('.gemara .large-type').first.text_content()=='שמאי'
    assert tab.locator('.gemara .section-start').all_text_contents()==['גמ׳']
    assert tab.locator('#stream-gemara').text_content().startswith('שמאי אומר')
    assert not tab.locator('.commentary .section-start').count()
    phrase=tab.evaluate("""() => {
      const s=current.streams.find(s=>s.id==='tosafot');
      const [start,length]=s.source.fragments.find(f=>f[1]>28);
      return Array.from(s.text).slice(start,start+length).join('').split(' ').slice(0,4).join(' ');
    }""")
    tab.locator('#query').fill(phrase);tab.locator('#searchForm button').click()
    assert tab.locator('.commentary .match').count()>=4
    tab.evaluate("delete window.copiedText")
    tab.locator('#copy').click();tab.wait_for_function('window.copiedText')
    copied=tab.evaluate('window.copiedText')
    label=tab.locator('#stream-gemara').get_attribute('aria-label')
    assert phrase in copied and f'\n{label}\nשמאי אומר' in copied
    tab.locator('#rashiFont').uncheck();assert not tab.evaluate("document.body.classList.contains('use-rashi')")
    tab.locator('#rashiFont').check();tab.locator('#theme').click();assert tab.evaluate("document.body.classList.contains('dark')")
    tab.locator('#larger').click();assert tab.locator('.page-facsimile').is_visible()
    tab.locator('#notes').uncheck();assert tab.locator('.reference-note').count()==0
    tab.set_viewport_size({'width':390,'height':844})
    assert tab.locator('.page-facsimile').evaluate('(s)=>parseFloat(getComputedStyle(s).width)')<400
    browser.close()
server.shutdown();server.server_close()
assert not report['browserErrors']
report['commentary']=dict(commentary_totals)
report['gemaraHeadings']=dict(gemara_sections)
report['illustrations']=dict(illustration_totals)
(ROOT/'qa/sefaria-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
quality=json.loads((ROOT/'quality.json').read_text(encoding='utf8'))
quality.update(books=len(catalog),pages=report['pages'],**{k:v for k,v in totals.items() if k not in ('positionedHeadings','sourceClassificationRepairs')},dataBytes=sum(b['bytes'] for b in catalog),validationPassed=True,checkedBrowserPages=len(report['browserPages']),browserCoordinateErrors=sum(s['clipped'] for s in report['browserPages']),browserScriptErrors=report['browserErrors'],layout='Sefaria Gemara fitted to source row regions; commentary retains source positions',sefariaConvertedPages=report['convertedPages'],sourceClassificationRepairs=totals['sourceClassificationRepairs'])
quality['headingsAndReferences']={'pages':report['pages'],'positionedHeadings':totals['positionedHeadings'],'embeddedHeadings':0,'referenceColumns':0,'failures':[]}
quality.update(layout='Sefaria Gemara and aligned commentary within printed regions',
               sefariaCommentary=dict(commentary_totals),gemaraHeadings=dict(gemara_sections),
               illustrations=dict(illustration_totals))
quality['packageBytes']=sum(f.stat().st_size for f in ROOT.rglob('*') if f.is_file() and not any(v in ('.git','.sefaria-cache','__pycache__') for v in f.relative_to(ROOT).parts) and f.suffix!='.png')
(ROOT/'quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
print(json.dumps({**report,'browserPages':len(report['browserPages'])},ensure_ascii=False))
