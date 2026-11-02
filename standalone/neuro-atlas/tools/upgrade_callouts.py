from pathlib import Path
root=Path(__file__).resolve().parent.parent
p=root/'dist/index.html';s=p.read_text(encoding='utf-8')
s=s.replace('<div id="annotations" class="annotation-list"></div>','''<div id="annotations" class="annotation-list"></div>
<div class="recovery"><button id="restore-deleted" class="secondary" disabled>Rétablir la dernière suppression</button></div>
<section class="review-section"><div class="section-title">À PRÉCISER AVEC VOUS <span id="review-count"></span></div><p>Ces libellés sont incomplets dans la vidéo. Précisez le nom et, si nécessaire, le point ciblé.</p><div id="review-list"></div></section>''')
s=s.replace('<div id="load-error"','<div id="callouts"></div><div id="load-error"')
s=s.replace('<div class="dialog-buttons"><button id="delete-annotation"','<label class="needs-review"><input id="needs-review" type="checkbox"> Ce nom ou cette position reste à préciser</label><div class="dialog-buttons"><button id="reposition-annotation" type="button" class="secondary">Replacer le point</button><button id="delete-annotation"')
s=s.replace('<div id="toast"', '''<dialog id="review-dialog"><form id="review-form"><div class="dialog-top"><span class="eyebrow">VOTRE PRÉCISION</span><button id="close-review" type="button" class="icon" aria-label="Fermer la demande">×</button></div><h2 id="review-title"></h2><p id="review-question"></p><a id="review-source" target="_blank" rel="noopener">Voir l’image de la vidéo ↗</a><label>Nom complet ou correction<input id="review-answer" maxlength="120" required></label><label>Votre commentaire<textarea id="review-note" maxlength="2000" rows="3"></textarea></label><p>Après l’enregistrement, cliquez sur « Placer ma réponse », puis sur la zone exacte de l’image. Aucune position n’est devinée.</p><button class="primary" type="submit">Enregistrer ma réponse</button></form></dialog>
<div id="toast"''')
p.write_text(s,encoding='utf-8')
p=root/'dist/app.js';s=p.read_text(encoding='utf-8')
s=s.replace("import {videoAnnotations}","import {layoutColumn} from './callout-layout.js';\nimport {videoAnnotations}")
s=s.replace("let hiddenBase=new Set();","let hiddenBase=new Set();\nlet deletedAnnotations=[],reviewState={},reviewing=null,repositioning=null,placingReview=null;\nconst reviewKey=key+'.reviews';\ntry{const d=JSON.parse(localStorage.getItem(key+'.deleted')||'[]');deletedAnnotations=validateAnnotations({version:1,annotations:d});}catch{}\ntry{const saved=JSON.parse(localStorage.getItem(reviewKey)||'{}');if(saved&&typeof saved==='object'&&!Array.isArray(saved))reviewState=saved;}catch{}")
s=s.replace("localStorage.setItem(key+'.hidden',JSON.stringify([...hiddenBase]));", "localStorage.setItem(key+'.hidden',JSON.stringify([...hiddenBase]));localStorage.setItem(key+'.deleted',JSON.stringify(deletedAnnotations));updateRestore();")
start=s.index('function renderMarkers()');end=s.index('function renderList()',start)
s=s[:start]+'''function renderMarkers(){
 const list=annotations.filter(a=>a.slice===slice&&matching(a));
 const w=stage.clientWidth,rail=Math.max(105,Math.min(235,w*.23)),available=w-rail*2-44;
 const lineChars=Math.max(13,Math.floor((rail-18)/6.4));
 const groups=[[],[]];
 list.forEach((a,i)=>{const side=a.x<.5?0:1;groups[side].push({a,index:i,side,height:Math.max(31,Math.ceil((quiz&&!revealed.has(a.id)?12:a.label.length)/lineChars)*17+12)});});
 const required=Math.max(...groups.map(g=>g.reduce((sum,item)=>sum+item.height+8,0)))+95;
 const base=Math.max(390,Math.min(670,window.innerHeight*.56));
 stage.style.height=Math.max(base,required)+'px';
 let box=imageRect();
 if(w<650){const ratio=Math.min(available/740,(stage.clientHeight-80)/960);box={w:1920*ratio,h:1080*ratio,left:w/2-965*ratio,top:stage.clientHeight/2-540*ratio};}
 else {const ratio=Math.min(available/740,(stage.clientHeight-80)/960);box={w:1920*ratio,h:1080*ratio,left:w/2-965*ratio,top:stage.clientHeight/2-540*ratio};}
 plane.dataset.box=JSON.stringify(box);
 scan.style.width=box.w+'px';scan.style.height=box.h+'px';scan.style.left=box.left+'px';scan.style.top=box.top+'px';scan.style.objectFit='fill';scan.style.clipPath='inset(6% 0 6% 0)';
 $('markers').replaceChildren();$('callouts').replaceChildren();if(!showLabels)return;
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox',`0 0 ${w} ${stage.clientHeight}`);svg.classList.add('leader-lines');
 const defs=document.createElementNS(svg.namespaceURI,'defs');const arrow=document.createElementNS(svg.namespaceURI,'marker');arrow.id='target-arrow';arrow.setAttribute('viewBox','0 0 10 10');arrow.setAttribute('refX','9');arrow.setAttribute('refY','5');arrow.setAttribute('markerWidth','7');arrow.setAttribute('markerHeight','7');arrow.setAttribute('orient','auto-start-reverse');const tip=document.createElementNS(svg.namespaceURI,'path');tip.setAttribute('d','M 0 0 L 10 5 L 0 10 z');tip.setAttribute('fill','context-stroke');arrow.append(tip);defs.append(arrow);svg.append(defs);$('callouts').append(svg);
 for(const group of groups){for(const item of group){item.targetX=(box.left+item.a.x*box.w-w/2)*zoom+w/2+dx;item.targetY=(box.top+item.a.y*box.h-stage.clientHeight/2)*zoom+stage.clientHeight/2+dy;}
 for(const item of layoutColumn(group,42,stage.clientHeight-40)){
  const a=item.a,color=colors[a.category]||'#72dbca',x=item.side===0?12:w-rail-12,anchorX=item.side===0?x+rail:x,anchorY=item.labelY+item.height/2;
  const button=document.createElement('button');button.className='external-label marker';button.dataset.annotationId=a.id;button.style.left=x+'px';button.style.top=item.labelY+'px';button.style.width=rail+'px';button.style.height=item.height+'px';button.style.setProperty('--marker-color',color);
  const hidden=quiz&&!revealed.has(a.id);button.textContent=hidden?`Repère ${item.index+1}`:a.label;button.setAttribute('aria-label',hidden?`Révéler le repère ${item.index+1}`:a.label);button.onclick=e=>{e.stopPropagation();if(quiz){revealed.add(a.id);renderMarkers();}else openAnnotation(a);};button.onpointerdown=e=>e.stopPropagation();$('callouts').append(button);
  if(item.targetX<rail+18||item.targetX>w-rail-18||item.targetY<32||item.targetY>stage.clientHeight-32){button.classList.add('offscreen');button.title='Point hors du champ. Réinitialisez le zoom pour le retrouver.';continue;}
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',`M ${anchorX} ${anchorY} L ${anchorX+(item.side===0?12:-12)} ${anchorY} L ${item.targetX} ${item.targetY}`);path.setAttribute('stroke',color);path.setAttribute('marker-end','url(#target-arrow)');svg.append(path);
  const dot=document.createElementNS(svg.namespaceURI,'circle');dot.setAttribute('cx',item.targetX);dot.setAttribute('cy',item.targetY);dot.setAttribute('r','2');dot.setAttribute('fill',color);svg.append(dot);
 }
 }
}
''' +s[end:]
s=s.replace("const noDocumented=onSlice.length===0", "const noDocumented=onSlice.length===0")
s=s.replace('La vidéo ne la documente pas suffisamment.','Cette coupe ne possède pas encore de transcription.')
s=s.replace("const d=$('annotation-dialog');", "const d=$('annotation-dialog');$('needs-review').checked=!!reviewState['annotation:'+a?.id];$('reposition-annotation').hidden=!a;")
s=s.replace("save();$('annotation-dialog').close();renderMarkers();renderList();toast('Repère enregistré · à valider');", "if($('needs-review').checked)reviewState['annotation:'+annotation.id]={status:'pending',answer:'',note:''};else delete reviewState['annotation:'+annotation.id];saveReviews();save();$('annotation-dialog').close();renderMarkers();renderList();renderReviews();toast('Repère enregistré · à valider');")
s=s.replace("const removed=editing;if(baseIds", "const removed=editing;deletedAnnotations=deletedAnnotations.filter(a=>a.id!==removed.id);deletedAnnotations.push(removed);if(baseIds")
s=s.replace("if(mode!=='annotate'||distance>6)return;const rect=stage.getBoundingClientRect(),box=imageRect();", "if(mode!=='annotate'||distance>6)return;const rect=stage.getBoundingClientRect(),box=JSON.parse(plane.dataset.box||'null')||imageRect();")
s=s.replace("pending={x,y};openAnnotation();", "pending={x,y};if(repositioning){annotations=annotations.map(a=>a.id===repositioning?{...a,x,y}:a);repositioning=null;setMode('pan');save();renderMarkers();renderList();toast('Point replacé');return;}if(placingReview){const r=placingReview;placingReview=null;const entry=reviewState[r.id];annotations.push({id:crypto.randomUUID(),slice,x,y,label:entry.answer,category:'Autre',note:`Réponse utilisateur à : ${r.question}. ${entry.note||''}`});entry.status='placed';saveReviews();save();setMode('pan');renderMarkers();renderList();renderReviews();toast('Votre précision a été placée');return;}openAnnotation();")
s=s.replace("mode=m;stage.classList", "mode=m;if(m==='pan'){repositioning=null;placingReview=null;}stage.classList")
s=s.replace("setSlice(5);transform();", "setSlice(5);transform();updateRestore();renderReviews();")
p.write_text(s,encoding='utf-8')
