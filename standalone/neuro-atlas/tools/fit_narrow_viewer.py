from pathlib import Path
root=Path(__file__).resolve().parent.parent
p=root/'dist/app.js';s=p.read_text(encoding='utf-8')
s=s.replace("const w=stage.clientWidth,h=stage.clientHeight,rail=Math.max(92,Math.min(235,w*.23)),available=Math.max(60,w-rail*2-44);", "const w=stage.clientWidth,h=stage.clientHeight,narrow=w<520,rail=narrow?(w-28)/2:Math.max(92,Math.min(235,w*.23)),railHeight=narrow?Math.min(145,h*.35):0,available=narrow?w-32:Math.max(60,w-rail*2-44);")
s=s.replace("Math.max(50,h-68)/960", "Math.max(50,h-railHeight-68)/960")
s=s.replace("top:h/2-540*ratio", "top:(h-railHeight)/2-540*ratio")
s=s.replace("column.style.width=rail+'px';", "column.style.width=rail+'px';if(narrow){column.style.top='auto';column.style.height=railHeight+'px';}")
s=s.replace("anchorY=38+labelTop+item.height/2,anchorX=item.side===0?10+rail:w-rail-10", "anchorY=column.offsetTop+labelTop+item.height/2,anchorX=narrow?(item.side===0?10+rail/2:w-10-rail/2):(item.side===0?10+rail:w-rail-10)")
s=s.replace("item.targetX<rail+18||item.targetX>w-rail-18", "item.targetX<(narrow?15:rail+18)||item.targetX>w-(narrow?15:rail+18)")
s=s.replace("$('coverage').textContent=", "$('library').onclick=()=>{const open=document.body.classList.toggle('sidebar-open');$('library').setAttribute('aria-expanded',String(open));};\n$('sidebar-shade').onclick=()=>{document.body.classList.remove('sidebar-open');$('library').setAttribute('aria-expanded','false');};\n$('coverage').textContent=")
p.write_text(s,encoding='utf-8')
p=root/'dist/index.html';s=p.read_text(encoding='utf-8');s=s.replace('<header><div class="brand">','<header><button id="library" class="icon" aria-label="Afficher la bibliothèque" aria-expanded="false" aria-controls="sidebar">☰</button><div class="brand">');s=s.replace('<main>','<button id="sidebar-shade" aria-label="Fermer la bibliothèque" tabindex="-1"></button><main>');p.write_text(s,encoding='utf-8')
p=root/'dist/callouts.css';s=p.read_text(encoding='utf-8');s+='''
#library,#sidebar-shade{display:none}
@media(max-width:700px){html,body{height:100%;overflow:hidden}header{position:relative;top:auto;gap:10px;padding:0 10px}header .brand{font-size:13px}header .brand small{font-size:5px}header .local{font-size:9px}#library{display:block;font-size:18px}main{display:block;height:calc(100dvh - 68px);min-height:0}.workspace{position:relative;top:auto;height:100%;width:100%;z-index:1;overflow:hidden}aside{position:fixed;top:68px;bottom:0;left:0;width:min(320px,88vw);height:auto;overflow:auto;background:var(--bg);z-index:30;transform:translateX(-105%);border-right:1px solid var(--line)}.sidebar-open aside{transform:none}.sidebar-open #sidebar-shade{display:block;position:fixed;inset:68px 0 0;border:0;background:#0009;z-index:20}.workspace footer{font-size:7px}.viewer-header h2{font-size:18px}.adjustments{flex-wrap:nowrap;gap:8px}.adjustments label{gap:5px;font-size:9px}.adjustments input{width:35px}.adjustments a{font-size:8px;margin-left:auto}.region-opacity{font-size:10px;padding:6px 10px}.region-opacity input{width:95px}.workspace .thumbnails{padding:6px 0 4px}.label-column{background:#07100ef5}.label-column .external-label.marker{background:#12211fee}}
''';p.write_text(s,encoding='utf-8')
print('Narrow preview fitted without page scrolling')
