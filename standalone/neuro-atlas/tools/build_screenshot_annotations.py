from pathlib import Path
import json,re,unicodedata,difflib,shutil
root=Path(__file__).resolve().parent.parent
old=json.loads((root/'dist/annotations-video.json').read_text(encoding='utf-8'))['annotations']
ocr=json.loads((root/'evidence/screenshots-ocr.json').read_text(encoding='utf-8'))
geo=json.loads((root/'evidence/screenshots-geometry.json').read_text(encoding='utf-8'))
extra='''Gyrus frontal médial|Gyrus précentral|Gyrus post-central|Gyrus supramarginal|Lobule pariétal inférieur|Lobule pariétal supérieur|Gyrus angulaire|Précuneus|Cuneus|Sillon intrapariétal|Sillon précentral|Sillon post-central|Sillon central|Sillon latéral|Sillon du cingulum|Septum pellucidum|Tronc du corps calleux|Genou du corps calleux|Rostre du corps calleux|Splenium du corps calleux|Corps du fornix|Piliers du fornix|Fornix|Commissure de l'hippocampe|Commissure antérieure|Foramen interventriculaire|Tête du noyau caudé|Corps du noyau caudé|Queue du noyau caudé|Partie centrale du ventricule latéral|Bras antérieur de la capsule interne|Bras postérieur de la capsule interne|Genou de la capsule interne|Capsule externe|Putamen|Thalamus|Globus pallidus latéral|Claustrum|Adhésion interthalamique|Pulvinar|Glande pinéale|Atrium du ventricule latéral|Plexus choroïde du troisième ventricule|Plexus choroïde|Gyrus occipitotemporal latéral|Gyrus occipitotemporal médial|Gyri courts de l'insula|Gyrus long de l'insula|Sillon central de l'insula|Sillon circulaire de l'insula|Limen de l'insula|Lobule central du vermis [II-III]|Lobule simple [VI et H VI]|Lobule semilunaire supérieur|Lobule semilunaire inférieur|Lobule digastrique [H VIII]|Pyramide du vermis [VIII]|Nodule du vermis [X]|Uvule du vermis [IX]|Folium vermis [VIIA]|Tuber vermis [VIIB]|Déclive [VI]|Culmen [IV-V]|Nerf optique [II]|Hypophyse|Ouverture latérale du quatrième ventricule|Ouverture médiane du quatrième ventricule|Citerne cérébellomédullaire…|Tonsille cérébelleuse ; paraflocculus ventral [H…|Lobe frontal|Lobe pariétal|Lobe temporal|Lobe occipital|Insula|Diencéphale|Mésencéphale|Pont|Moelle allongée|Moelle spinale|Lobe cérébelleux antérieur|Lobe cérébelleux postérieur|Lobe flocculo-nodulaire'''.split('|')
def norm(s):return re.sub('[^a-z0-9]','',unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode().lower())
extra+=['Corne frontale','Corne occipitale','Grande veine cérébrale','Sinus pétreux supérieur','Pédoncule cérébelleux supérieur']
names=sorted(set(extra+[a['label'] for a in old]))
def canonical(raw):
 if raw=='Sinu':return 'Sinus sagittal supérieur',1
 if norm(raw).startswith('lobulecentralduvermis'):return 'Lobule central du vermis [II-III]',1
 if norm(raw).startswith('citernecerebellomedullaire'):return 'Citerne cérébellomédullaire…',1
 if norm(raw).startswith('tonsillecerebelleuse'):return 'Tonsille cérébelleuse ; paraflocculus ventral [H…',1
 best=max(names,key=lambda label:difflib.SequenceMatcher(None,norm(raw),norm(label)).ratio())
 score=difflib.SequenceMatcher(None,norm(raw),norm(best)).ratio()
 return best,score
palette={'#3399ff':'Sinus veineux','#ff0000':'Artères','#cccccc':'Gyri encéphaliques','#ffcc99':'Sillons encéphaliques','#cc99ff':'Système limbique','#ffffcc':'Voies optiques','#ccff99':'Tronc cérébral','#ffccff':'Cervelet','#ccffff':'Ventricules / Citernes','#ccffcc':'Noyaux de la base'}
def cat(label,color,regions):
 if regions:return 'Régions'
 if label=='Hypophyse':return 'Glandes'
 if label.startswith(('Sinus','Veine','Grande veine','Plexus veineux')):return 'Sinus veineux'
 if label.startswith('Artère'):return 'Artères'
 if any(s in label for s in ['noyau caudé','Putamen','Claustrum','Globus pallidus','Thalamus','Pulvinar','Adhésion interthalamique']):return 'Noyaux de la base'
 if any(s in label for s in ['calleux','capsule','Capsule','Couronne','Fornix','fornix','Commissure']):return 'Substance blanche'
 rgb=[min(255,round(c/.75)) for c in color]
 return min(palette,key=lambda h:sum((rgb[i]-int(h[1+i*2:3+i*2],16))**2 for i in range(3))) and palette[min(palette,key=lambda h:sum((rgb[i]-int(h[1+i*2:3+i*2],16))**2 for i in range(3)))]
data=[];report=[];used=set()
for image in geo:
 source=next(i for i in ocr if i['file']==image['file']);regions=source['regions'];sl=image['slice']
 for index,anchor in enumerate(image['anchors']):
  ts=[t for t in source['texts'] if t['side']==anchor['side'] and min([a for a in image['anchors'] if a['side']==t['side']],key=lambda a:abs(t['y']-a['y'])) is anchor]
  raw=' '.join(t['label'] for t in sorted(ts,key=lambda t:t['y']))
  label,score=canonical(raw)
  point=anchor['point']
  if image['file']==43 and index==27:point=[1016,860]
  if image['file']==40 and index==35:point=[936,395]
  if image['file']==42 and index==34:point=[900,738]
  if image['file']==30 and index==1:point=[877.8,452.2]
  if image['file']==44 and index==15:point=[958.2,487.8]
  previous=[a for a in old if a['slice']==sl and norm(a['label'])==norm(label) and a['id'] not in used] if not regions else []
  prior=min(previous,key=lambda a:abs(a['y']*1080-13-(point[1] if point else anchor['y']))) if previous else None
  if prior:used.add(prior['id'])
  if not point and prior:point=[prior['x']*1920-70,prior['y']*1080-13]
  report.append(dict(file=image['file'],slice=sl,regions=regions,index=index,side=anchor['side'],anchorY=anchor['y'],raw=raw,label=label,score=round(score,3),point=point,dotVerified=anchor.get('dotVerified',False),prior=prior['id'] if prior else None,priorDistance=round(((prior['x']*1920-70-point[0])**2+(prior['y']*1080-13-point[1])**2)**.5,1) if prior and point else None))
  if not point:continue
  # Existing manually transcribed video coordinates take precedence for the same label.
  if prior and not anchor.get('dotVerified',False):point=[round(prior['x']*1920-70,1),round(prior['y']*1080-13,1)]
  ident=prior['id'] if prior else f'screenshot-v1-{image["file"]}-{index:02}'
  a=dict(id=ident,slice=sl,label=label,category=cat(label,anchor['color'],regions),x=round((point[0]+70)/1920,7),y=round((point[1]+13)/1080,7),note=f'Transcrit de la capture fournie ({image["file"]}), coupe {sl}/24. '+('Région colorée. ' if regions else '')+'À vérifier.'+((' Libellé tronqué dans la capture.') if '…' in label else ''),source=dict(kind='user-screenshot',file=image['file'],frameSha256=source['sha256'],pointInFrame=point,registration=[70,13],status='transcribed-not-clinically-validated'))
  data.append(a)
# Preserve every previously available seed ID, even where the screenshot text differs.
for a in old:
 if a['id'] not in used:data.append(a)
(root/'evidence/screenshots-transcription-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
(root/'dist/screenshot-annotations.js').write_text('export const screenshotAnnotations = '+json.dumps(data,ensure_ascii=False,indent=2)+';\n',encoding='utf-8')
(root/'dist/annotations-completes.json').write_text(json.dumps(dict(version=1,annotations=data),ensure_ascii=False,indent=2),encoding='utf-8')
print('Total',len(data),'regions',sum(a['category']=='Régions' for a in data),'old unmatched',len(old)-len(used))
print('LOW OCR',[(r['slice'],r['raw'],r['label'],r['score']) for r in report if r['score']<.87])
print('MISSING',[(r['file'],r['index'],r['raw']) for r in report if not r['point']])
print('DOT UNVERIFIED',[(r['file'],r['index'],r['label'],r['point']) for r in report if not r['dotVerified'] and not r['prior']])
