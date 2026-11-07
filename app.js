'use strict';
const $=s=>document.querySelector(s),cache=new Map();let catalog=[],pages=[],bookIndex=0,pageNumber=1,serial=0,current,searchTerm='';
const status=s=>$('#status').textContent=s;
const hebrew=n=>{let out='';for(const [v,c] of [[400,'ת'],[300,'ש'],[200,'ר'],[100,'ק'],[90,'צ'],[80,'פ'],[70,'ע'],[60,'ס'],[50,'נ'],[40,'מ'],[30,'ל'],[20,'כ'],[10,'י'],[9,'ט'],[8,'ח'],[7,'ז'],[6,'ו'],[5,'ה'],[4,'ד'],[3,'ג'],[2,'ב'],[1,'א']])while(n>=v){out+=c;n-=v}return out.replace('יה','טו').replace('יו','טז')};
const fallbackDaf=n=>`${hebrew(2+Math.floor((n-1)/2))} ע״${n%2?'א':'ב'}`;
const daf=n=>pages[n-1]?.daf||fallbackDaf(n);
function hashState(){const q=new URLSearchParams(location.hash.slice(1));return [Math.max(0,catalog.findIndex(b=>b.id===Number(q.get('book')??catalog[0]?.id))),Math.max(1,Number(q.get('page'))||1)]}
async function getBook(i){const b=catalog[i];if(cache.has(b.id))return cache.get(b.id);const response=await fetch(b.path);if(!response.ok)throw Error('לא ניתן לטעון את המסכת');const bytes=new Uint8Array(await response.arrayBuffer());let text;if(bytes[0]===31&&bytes[1]===139){if(!window.DecompressionStream)throw Error('נדרש דפדפן מעודכן לקריאת המסכת');text=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text()}else text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);const data=JSON.parse(text);cache.set(b.id,data);if(cache.size>3){const first=cache.keys().next().value;if(first!==b.id)cache.delete(first)}return data}
const svgNode=(name,attrs={})=>{const node=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [k,v] of Object.entries(attrs))node.setAttribute(k,v);return node};
const visibleStreams=page=>page.streams.filter(s=>!['note-מסורתהשס','note-עיןמשפט'].includes(s.id)&&!['note-מסורתהשס','note-עיןמשפט'].includes(s.parent));
let illustrationMask=0;
function illustrationImage(image,box,attributes={}){
 const [x,y,width,height]=box;
 if(!image.crop)return svgNode('image',{x,y,width,height,href:image.src,...attributes});
 const picture=svgNode('svg',{x,y,width,height,viewBox:image.crop.join(' '),overflow:'hidden',...attributes});
 const bitmap=svgNode('image',{width:image.fileSize[0],height:image.fileSize[1],href:image.src});
 if(image.clip){
  const id=`illustration-mask-${++illustrationMask}`;
  const clip=svgNode('clipPath',{id});
  clip.append(svgNode('polygon',{points:image.clip.map(point=>point.join(',')).join(' ')}));
  picture.append(clip);
  bitmap.setAttribute('clip-path',`url(#${id})`);
 }
 picture.append(bitmap);
 return picture;
}
function renderIllustrations(svg,page){
 const images=[...page.figures.flatMap(slot=>slot.images??[]),...(page.illustrations??[])];
 const order=new Map(page.streams.map((stream,i)=>[stream.id,i]));
 const refs=new Intl.Collator('en',{numeric:true});
 images.sort((a,b)=>(order.get(a.stream)??Infinity)-(order.get(b.stream)??Infinity)
   || (a.at??Infinity)-(b.at??Infinity) || refs.compare(a.ref,b.ref) || a.index-b.index);
 const numbers=new Map(images.map((image,i)=>[image,i+1]));
 const bounds=box=>box.map((value,i)=>value*(i%2?page.height:page.width));
 function goTo(element){
  element?.scrollIntoView({behavior:'smooth',block:'center'});
  element?.focus({preventScroll:true});
 }
 function linkedImage(image,node){
  const number=numbers.get(image),id=`illustration-${number}`;
  const link=svgNode('a',{href:`#${id}`,class:'illustration-link','aria-label':`איור ${number} · ${image.label}`});
  link.addEventListener('click',event=>{event.preventDefault();goTo(document.getElementById(id))});
  link.append(node);
  return link;
 }
 for(const image of images){
  if(!image.box)continue;
  const [x,y,width,height]=bounds(image.box);
  const scale=Math.min(width/image.size[0],height/image.size[1]);
  const w=image.size[0]*scale,h=image.size[1]*scale;
  const group=svgNode('g');
  group.append(svgNode('rect',{x:x+(width-w)/2,y:y+(height-h)/2,width:w,height:h,class:'illustration-backdrop'}));
  group.append(illustrationImage(image,[x,y,width,height],{class:'page-illustration','data-stream':image.stream}));
  group.append(svgNode('rect',{x,y,width,height,class:'illustration-hit'}));
  svg.append(linkedImage(image,group));
 }
 for(const slot of page.figures){
  if(slot.images?.length)continue;
  const [x,y,width,height]=bounds(slot.box);
  const group=svgNode('g',{class:'figure-position'});
  group.append(svgNode('rect',{x,y,width,height}));
  const label=svgNode('text',{x:x+width/2,y:y+height/2,'text-anchor':'middle','font-size':Math.min(7,width/9)});
  label.textContent='מקום איור';group.append(label);svg.append(group);
 }
 if(!images.length)return;
 const gallery=document.createElement('section');
 gallery.className='illustration-gallery';gallery.setAttribute('aria-label','איורים בדף');
 const heading=document.createElement('h2');heading.textContent='איורים בדף';
 const cards=document.createElement('div');cards.className='illustration-cards';
 gallery.append(heading,cards);
 const streams=new Map(page.streams.map(stream=>[stream.id,stream]));
 const margins=[[],[]];
 const markerRadius=6;
 for(const image of images){
  const number=numbers.get(image);
  const card=document.createElement('figure');
  card.id=`illustration-${number}`;card.tabIndex=-1;
  const captionText=`איור ${number} · ${image.label}`;
  const scale=Math.min(2,500/Math.max(...image.size));
  const [width,height]=image.size.map(size=>Math.max(1,Math.round(size*scale)));
  const picture=image.crop
   ?illustrationImage(image,[0,0,width,height],{role:'img','aria-label':captionText})
   :document.createElement('img');
  if(!image.crop){
   picture.src=image.src;picture.alt=captionText;picture.width=width;picture.height=height;picture.loading='lazy';
  }
  const caption=document.createElement('figcaption');
  const label=document.createElement('strong');label.textContent=captionText;
  caption.append(label);
  if(image.context){
   const quote=document.createElement('span');quote.textContent=image.context;
   caption.append(quote);
  }
  if(image.fileSource){
   const {file,author,license}=image.fileSource;
   const source=document.createElement('a');
   source.href=file;source.textContent=['קובץ המקור',author,license].filter(Boolean).join(' · ');
   caption.append(source);
  }
  const stream=streams.get(image.stream);
  const anchor=image.at==null?null:stream?.runs.findLast(run=>run[0]<image.at&&!run[6]);
  if(anchor){
   const back=document.createElement('button');back.type='button';
   back.textContent=image.stream==='gemara'?'למקום בגמרא':'למקום בפירוש';
   back.onclick=()=>{
    const span=document.getElementById(`stream-${stream.id}`)?.querySelector(`[data-start="${anchor[0]}"]`);
    goTo(span);
    if(span){span.classList.add('flash');setTimeout(()=>span.classList.remove('flash'),2000)}
   };
   caption.append(back);
  }
  card.append(picture,caption);cards.append(card);
  if(image.box||!anchor)continue;
  const side=anchor[2]<page.width/2?0:1;
  const x=side?page.width-2*markerRadius:2*markerRadius;
  let y=Math.max(markerRadius,anchor[4]-4);
  while(margins[side].some(existing=>Math.abs(existing-y)<2*markerRadius+2))y+=2*markerRadius+2;
  if(y+markerRadius>page.height)continue;
  const overlaps=page.streams.some(s=>s.runs.some(r=>x+markerRadius>=r[2]&&x-markerRadius<=r[3]
    &&y+markerRadius>=r[4]-r[5]&&y-markerRadius<=r[4]+r[5]*.25));
  if(overlaps)continue;
  margins[side].push(y);
  const marker=svgNode('g',{class:'illustration-marker'});
  marker.append(svgNode('circle',{cx:x,cy:y,r:markerRadius}));
  const numeral=svgNode('text',{x,y:y+2.3,'text-anchor':'middle','font-size':6.6});
  numeral.textContent=number;marker.append(numeral);svg.append(linkedImage(image,marker));
 }
 const navigation=document.createElement('a');
 navigation.href='#illustration-1';navigation.textContent=`איורים (${images.length})`;
 navigation.onclick=event=>{event.preventDefault();goTo(cards.firstElementChild)};
 $('#streamNav').append(navigation);
 $('#sheet').append(gallery);
}
function render(){
 current=pages[pageNumber-1];$('#bookTitle').textContent=`מסכת ${catalog[bookIndex].title}`;$('#pageTitle').textContent=`דף ${daf(pageNumber)}`;document.title=`${catalog[bookIndex].title} ${daf(pageNumber)} · דף פתוח`;$('#sheet').replaceChildren();$('#streamNav').replaceChildren();
 if(!current.width||current.streams.some(s=>!s.runs)){status('נתוני שחזור צורת הדף נמצאים בבנייה.');return}
 const svg=svgNode('svg',{viewBox:`0 0 ${current.width} ${current.height}`,class:'page-facsimile',role:'img','aria-label':`${catalog[bookIndex].title} ${daf(pageNumber)} — שחזור צורת הדף`});
 const title=svgNode('title');title.textContent=current.title;svg.append(title);
 for(const stream of visibleStreams(current)){
  const text=svgNode('text',{id:`stream-${stream.id}`,class:`positioned-stream ${stream.role}`,direction:'rtl','text-anchor':'start','aria-label':stream.label,'xml:space':'preserve'}),chars=Array.from(stream.text),matches=[],sectionStarts=new Set(stream.sectionStarts);let cursor=0;
  const headings=stream.headings??[];
  let headingIndex=0;
  if(searchTerm)for(let at=stream.text.indexOf(searchTerm);at>=0;at=stream.text.indexOf(searchTerm,at+1))matches.push([at,at+searchTerm.length]);
  for(const [start,length,left,right,y,fontSize,reference] of stream.runs){
   if(start>cursor)text.append(document.createTextNode(chars.slice(cursor,start).join('')));
   const value=chars.slice(start,start+length).join(''),width=Math.max(.01,right-left);
   const span=svgNode('tspan',{x:right,y,'font-size':fontSize,textLength:width,lengthAdjust:'spacingAndGlyphs','data-start':start,'data-length':length,'data-left':left,'data-right':right});
   if(reference)span.classList.add('reference');
   const sectionStart=sectionStarts.has(start);
   while(headingIndex<headings.length&&headings[headingIndex][0]+headings[headingIndex][1]<=start)headingIndex++;
   const heading=headings[headingIndex];
   const dibbur=heading&&heading[0]<start+length;
   if(sectionStart)span.classList.add('section-start');
   if(dibbur)span.classList.add('dibbur-hamatchil');
   if(dibbur||fontSize>18||sectionStart)span.setAttribute('font-weight',dibbur?'700':'600');
   if(!sectionStart&&(fontSize>30||(stream.role==='gemara'&&fontSize>=24)))span.classList.add('large-type');
   if(matches.some(([a,b])=>a<start+length&&b>start))span.classList.add('match');
   // Standalone source vowel glyphs have a measured box, but Unicode marks
   // have zero advance. An invisible base lets SVG fit that box correctly.
   span.textContent=/^\p{Mark}+$/u.test(value)?'\u00a0'+value:value;text.append(span);cursor=start+length;
  }
  svg.append(text);
  if(!stream.parent){const a=document.createElement('a');a.href=`#stream-${stream.id}`;a.textContent=stream.label;a.onclick=e=>{e.preventDefault();text.scrollIntoView({behavior:'smooth',block:'center'});text.classList.add('flash');setTimeout(()=>text.classList.remove('flash'),2000)};$('#streamNav').append(a)}
 }
 $('#sheet').append(svg);renderIllustrations(svg,current);$('#prev').disabled=pageNumber===1;$('#next').disabled=pageNumber===pages.length;$('#daf').value=String(pageNumber);$('#sheet').setAttribute('aria-busy','false');window.liteViewerState={book:catalog[bookIndex].id,page:pageNumber,streams:current.streams.length,figures:current.figures.length};status(`שחזור צורת הדף · ${pages.length} עמודים במסכת`)
}
async function navigate(i,n,writeHash=true){const ticket=++serial;status('טוען את המסכת…');$('#sheet').setAttribute('aria-busy','true');try{const data=await getBook(i);if(ticket!==serial)return;bookIndex=i;pages=data;pageNumber=Math.max(1,Math.min(data.length,n));$('#book').value=String(i);$('#daf').replaceChildren(...pages.map(p=>new Option(daf(p.n),p.n)));if(writeHash)history.replaceState(null,'',`#book=${catalog[i].id}&page=${pageNumber}`);render();localStorage.setItem('last-place',JSON.stringify([catalog[i].id,pageNumber]))}catch(error){status(error.message);$('#sheet').setAttribute('aria-busy','false')}}
$('#book').onchange=()=>{searchTerm='';$('#results').hidden=true;navigate(Number($('#book').value),1)};$('#daf').onchange=()=>navigate(bookIndex,Number($('#daf').value));$('#prev').onclick=()=>navigate(bookIndex,pageNumber-1);$('#next').onclick=()=>navigate(bookIndex,pageNumber+1);
$('#rashiFont').onchange=()=>document.body.classList.toggle('use-rashi',$('#rashiFont').checked);$('#notes').onchange=()=>document.body.classList.toggle('hide-notes',!$('#notes').checked);document.body.classList.add('use-rashi');
let size=Number(localStorage.getItem('page-zoom'))||100;function setSize(n){size=Math.max(50,Math.min(250,n));document.documentElement.style.setProperty('--page-zoom',size/100);localStorage.setItem('page-zoom',size)}setSize(size);$('#smaller').onclick=()=>setSize(size-10);$('#larger').onclick=()=>setSize(size+10);
document.body.classList.toggle('dark',localStorage.getItem('theme')==='dark');$('#theme').onclick=()=>{document.body.classList.toggle('dark');localStorage.setItem('theme',document.body.classList.contains('dark')?'dark':'light')};
$('#bookmark').onclick=()=>{localStorage.setItem('bookmark',JSON.stringify([catalog[bookIndex].id,pageNumber]));status(`המקום נשמר: ${catalog[bookIndex].title}, ${daf(pageNumber)}`)};
$('#copy').onclick=async()=>{try{await navigator.clipboard.writeText(`${catalog[bookIndex].title} · ${daf(pageNumber)}\n\n`+visibleStreams(current).map(s=>`${s.label}\n${s.text.trim()}`).join('\n\n'));status('הדף הועתק')}catch{status('הדפדפן חסם העתקה. אפשר לסמן ולהעתיק את הטקסט')}};
$('#searchForm').onsubmit=async e=>{e.preventDefault();searchTerm=$('#query').value.trim();$('#results').replaceChildren();$('#results').hidden=!searchTerm;if(!searchTerm){render();return}const term=searchTerm.toLocaleLowerCase('he');let count=0;for(const p of pages){for(const s of visibleStreams(p)){const at=s.text.toLocaleLowerCase('he').indexOf(term);if(at<0)continue;const button=document.createElement('button');button.className='result';const strong=document.createElement('strong');strong.textContent=`${daf(p.n)} · ${s.label}`;const snippet=document.createElement('span');snippet.textContent=s.text.slice(Math.max(0,at-55),at+term.length+85);button.append(strong,snippet);button.onclick=async()=>{await navigate(bookIndex,p.n);const target=$(`#stream-${s.parent||s.id}`);target?.scrollIntoView({behavior:'smooth'});target?.classList.add('flash')};$('#results').append(button);if(++count>=80)break}if(count>=80)break}status(count?`${count}${count===80?'+':''} תוצאות במסכת`:'לא נמצאו תוצאות במסכת');render();status(count?`${count}${count===80?'+':''} תוצאות במסכת`:'לא נמצאו תוצאות במסכת')};
window.addEventListener('hashchange',()=>{if(location.hash.startsWith('#book=')){const [i,n]=hashState();navigate(i,n,false)}});window.addEventListener('keydown',e=>{if(/INPUT|SELECT|TEXTAREA/.test(e.target.tagName))return;if(e.key==='ArrowLeft'&&pageNumber<pages.length)navigate(bookIndex,pageNumber+1);if(e.key==='ArrowRight'&&pageNumber>1)navigate(bookIndex,pageNumber-1)});
(async()=>{try{catalog=await (await fetch('catalog.json')).json();$('#book').replaceChildren(...catalog.map((b,i)=>new Option(b.title,i)));let [i,n]=hashState();if(!location.hash){const saved=JSON.parse(localStorage.getItem('bookmark')||localStorage.getItem('last-place')||'null');if(saved){i=Math.max(0,catalog.findIndex(b=>b.id===saved[0]));n=saved[1]}}await navigate(i,n)}catch(error){status(`טעינת הספרייה נכשלה: ${error.message}`)}})();
