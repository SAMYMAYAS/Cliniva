from pathlib import Path
root=Path(__file__).resolve().parent.parent
p=root/'dist/app.js';s=p.read_text(encoding='utf-8')
s=s.replace("let showRegions=false;", "let showRegions=false;\nlet railScroll=[0,0];\nconst layerVisible=a=>isRegion(a)?showRegions:showLabels;")
s=s.replace('(isRegion(a)===showRegions)&&','layerVisible(a)&&')
s=s.replace('a.slice===slice&&isRegion(a)===showRegions','a.slice===slice&&layerVisible(a)')
s=s.replace('slice=next;scan.src=', 'if(slice!==next)railScroll=[0,0];slice=next;scan.src=')
start=s.index('function renderMarkers(){');end=s.index('\nfunction renderList()',start)
s=s[:start]+'''function renderMarkers(){
 const list=annotations.filter(a=>a.slice===slice&&matching(a));
 const w=stage.clientWidth,h=stage.clientHeight,rail=Math.max(92,Math.min(235,w*.23)),available=Math.max(60,w-rail*2-44);
 const lineChars=Math.max(12,Math.floor((rail-18)/6.4)),groups=[[],[]];
 list.forEach((a,i)=>{const side=a.x<.5?0:1;groups[side].push({a,index:i,side,height:Math.max(26,Math.ceil((quiz&&!revealed.has(a.id)?12:a.label.length)/lineChars)*16+8)});});
 const ratio=Math.min(available/740,Math.max(50,h-68)/960);
 const box={w:1920*ratio,h:1080*ratio,left:w/2-965*ratio,top:h/2-540*ratio};
 plane.dataset.box=JSON.stringify(box);
 for(const img of [scan,$('region-overlay')])Object.assign(img.style,{width:box.w+'px',height:box.h+'px',left:box.left+'px',top:box.top+'px'});
 $('region-overlay').hidden=!showRegions;
 $('markers').replaceChildren();$('callouts').replaceChildren();
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox',`0 0 ${w} ${h}`);svg.classList.add('leader-lines');
 const defs=document.createElementNS(ns,'defs'),arrow=document.createElementNS(ns,'marker');arrow.id='target-arrow';arrow.setAttribute('viewBox','0 0 10 10');arrow.setAttribute('refX','9');arrow.setAttribute('refY','5');arrow.setAttribute('markerWidth','7');arrow.setAttribute('markerHeight','7');arrow.setAttribute('orient','auto-start-reverse');const tip=document.createElementNS(ns,'path');tip.setAttribute('d','M 0 0 L 10 5 L 0 10 z');tip.setAttribute('fill','context-stroke');arrow.append(tip);defs.append(arrow);svg.append(defs);
 const leaders=document.createElementNS(ns,'g');svg.append(leaders);$('callouts').append(svg);
 const entries=[],columns=[];
 groups.forEach((group,side)=>{
  const column=document.createElement('div');column.className='label-column';column.style.width=rail+'px';column.style[side===0?'left':'right']='10px';column.setAttribute('role','region');column.setAttribute('aria-label',`Légendes ${side===0?'à gauche':'à droite'}, défilement indépendant`);
  const content=document.createElement('div');content.className='label-column-content';column.append(content);$('callouts').append(column);columns[side]=column;
  for(const item of group){item.targetX=(box.left+item.a.x*box.w-w/2)*zoom+w/2+dx;item.targetY=(box.top+item.a.y*box.h-h/2)*zoom+h/2+dy;}
  const needed=group.reduce((sum,item)=>sum+item.height+6,0),overflow=needed>column.clientHeight;
  const placed=overflow?[...group].sort((a,b)=>a.targetY-b.targetY):layoutColumn(group,0,column.clientHeight,6);
  let cursor=0;
  for(const item of placed){
   if(overflow){item.labelY=cursor;cursor+=item.height+6;}
   const a=item.a,color=(isRegion(a)?({'Lobe frontal':'#a1ed38','Diencéphale':'#ffad70','Mésencéphale':'#75aaff','Pont':'#73d9ec','Moelle allongée':'#73d9ec','Lobe cérébelleux antérieur':'#ab87f1','Lobe cérébelleux postérieur':'#d0a9ee','Lobe flocculo-nodulaire':'#ce8bf1'}[a.label]||'#72dbca'):colors[a.category])||'#72dbca';
   const button=document.createElement('button');button.className='external-label marker';button.dataset.annotationId=a.id;Object.assign(button.style,{left:'0px',top:item.labelY+'px',width:'100%',height:item.height+'px'});button.style.setProperty('--marker-color',color);
   const hidden=quiz&&!revealed.has(a.id);button.textContent=hidden?`Repère ${item.index+1}`:a.label;button.setAttribute('aria-label',hidden?`Révéler le repère ${item.index+1}`:a.label);button.onclick=e=>{e.stopPropagation();if(quiz){revealed.add(a.id);renderMarkers();}else openAnnotation(a);};button.onpointerdown=e=>e.stopPropagation();content.append(button);entries.push({item,button,color,column});
  }
  content.style.height=(overflow?cursor:column.clientHeight)+'px';column.classList.toggle('scrollable',overflow);column.scrollTop=railScroll[side];
 });
 function drawLeaders(){
  leaders.replaceChildren();
  for(const {item,button,color,column} of entries){
   const labelTop=item.labelY-column.scrollTop,anchorY=38+labelTop+item.height/2,anchorX=item.side===0?10+rail:w-rail-10;
   const offscreen=item.targetX<rail+18||item.targetX>w-rail-18||item.targetY<30||item.targetY>h-30;button.classList.toggle('offscreen',offscreen);button.title=offscreen?'Point hors du champ. Réinitialisez le zoom pour le retrouver.':'';
   if(offscreen||labelTop<0||labelTop+item.height>column.clientHeight)continue;
   const path=document.createElementNS(ns,'path');path.setAttribute('d',`M ${anchorX} ${anchorY} L ${anchorX+(item.side===0?12:-12)} ${anchorY} L ${item.targetX} ${item.targetY}`);path.setAttribute('stroke',color);path.setAttribute('marker-end','url(#target-arrow)');leaders.append(path);
   const dot=document.createElementNS(ns,'circle');dot.setAttribute('cx',item.targetX);dot.setAttribute('cy',item.targetY);dot.setAttribute('r','2');dot.setAttribute('fill',color);leaders.append(dot);
  }
 }
 columns.forEach((column,side)=>column.onscroll=()=>{railScroll[side]=column.scrollTop;drawLeaders();});drawLeaders();
}
''' + s[end:]
s=s.replace("$('category').value='';$('search').value='';$('region-opacity-control')", "railScroll=[0,0];$('region-opacity-control')")
s=s.replace("$('labels').setAttribute('aria-pressed',String(showLabels));renderMarkers();", "$('labels').setAttribute('aria-pressed',String(showLabels));railScroll=[0,0];renderMarkers();renderList();")
s=s.replace("function refreshFilters(){renderList();renderMarkers();}","function refreshFilters(){railScroll=[0,0];renderList();renderMarkers();}")
s=s.replace("if(e.button!==0||e.target.closest('.marker'))return;", "if(e.button!==0||e.target.closest('.marker,.label-column'))return;")
s=s.replace("stage.onwheel=e=>{e.preventDefault();", "stage.onwheel=e=>{if(e.target.closest('.label-column'))return;e.preventDefault();")
s=s.replace("stage.addEventListener('wheel',e=>e.preventDefault(),{passive:false});", "stage.addEventListener('wheel',e=>{if(!e.target.closest('.label-column'))e.preventDefault();},{passive:false});")
p.write_text(s,encoding='utf-8')
p=root/'dist/callouts.css';s=p.read_text(encoding='utf-8');s+='''
/* The image never grows with the number of legends. Only the legend rails scroll. */
.leader-lines path{fill:none;stroke-width:1.2;opacity:.88}.label-column{position:absolute;top:38px;bottom:34px;overflow-y:auto;overflow-x:hidden;pointer-events:auto;scrollbar-width:thin;scrollbar-color:#45635c #0d1717;overscroll-behavior:contain}.label-column-content{position:relative;width:100%}.label-column .external-label.marker{font-size:11px;line-height:16px;padding:4px 7px}.label-column.scrollable{border-bottom:1px solid #45635c}
html,body{height:100%;overflow:hidden}header{height:64px;flex-shrink:0}main{height:calc(100dvh - 64px);min-height:0}aside{height:100%;max-height:none;position:static;min-height:0;overscroll-behavior:contain}.workspace{height:100%;min-height:0;padding:14px 20px 10px;overflow:hidden}.viewer-header{margin-bottom:10px;flex-shrink:0}.viewer-header h2{font-size:21px}.toolbar{flex-shrink:0}#stage{height:auto;min-height:0;flex:1 1 0}.adjustments,.region-opacity,.navigation,.thumbnails,footer{flex-shrink:0}.thumbnails{padding:9px 1px 5px}.thumb{height:48px}.navigation{margin-top:9px}footer{margin-top:5px}.region-opacity{padding:8px 14px}.workspace:fullscreen{height:100dvh}.workspace:fullscreen #stage{height:auto;min-height:0}
@media(max-height:760px){.workspace{padding-top:8px;padding-bottom:6px}.viewer-header{margin-bottom:6px}.viewer-header .eyebrow{display:none}.viewer-header h2{margin:0}.adjustments{padding:8px 12px}.thumbnails{padding-top:6px}.thumb{height:38px}footer{font-size:8px;line-height:1.2}.region-opacity{padding:6px 12px}}
@media(max-width:700px){html,body{height:auto;overflow:auto}header{height:68px;position:sticky;top:0;z-index:10;background:var(--bg)}main{height:auto;min-height:calc(100dvh - 68px)}.workspace{position:sticky;top:68px;height:calc(100dvh - 68px);min-height:0;background:var(--bg);z-index:6;padding:8px 10px}.workspace #stage{min-height:0;height:auto}.viewer-header{margin-bottom:6px}.viewer-header .eyebrow{display:none}.adjustments{gap:9px;padding:7px 10px}.thumbnails{padding-top:6px}.thumb{height:38px}.label-column .external-label.marker{font-size:10px}aside{height:auto;overflow:visible}.stage-top,.stage-bottom{left:10px;right:10px}}
''';p.write_text(s,encoding='utf-8')
print('Viewport and independent layers updated')
