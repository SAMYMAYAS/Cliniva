from pathlib import Path
import json,hashlib
root=Path(__file__).resolve().parent.parent
raw=json.loads((root/'tools/video_points.json').read_text(encoding='utf-8'))
registration=json.loads((root/'evidence/video/registration.json').read_text(encoding='utf-8'))
def category(label):
 if label.startswith(('Sinus','Veine','Plexus veineux')):return 'Sinus veineux'
 if label.startswith('Artère'):return 'Artères'
 if label in ['Nerf optique [II]','Chiasma optique','Tractus optique','Radiation optique']:return 'Voies optiques'
 if any(s in label for s in ['cingulum','parahippocampal','Uncus','amygdalienne','mamillaire','Archicortex','rhinal']):return 'Système limbique'
 if label.startswith(('Sillon','Fissure','Incisure')):return 'Sillons encéphaliques'
 if any(s in label for s in ['ventricule','ventriculaire','Citerne','Corne ','Aqueduc','Plexus choroïde','Espace subarachnoïdien']):return 'Ventricules / Citernes'
 if any(s in label for s in ['caudé','Putamen','Claustrum','Globus']):return 'Noyaux de la base'
 if any(s in label for s in ['calleux','capsule','Capsule','Couronne','Fornix','Commissure']):return 'Substance blanche'
 if any(s in label for s in ['cérébral','cérébelleux','Collicule','Tronc','rouge']):return 'Tronc cérébral'
 if any(s in label for s in ['cervelet','vermis','Lobule digastrique','Lobule semilunaire','Culmen','Déclive','Flocculus','Lobule quadrangulaire']):return 'Cervelet'
 return 'Gyri encéphaliques'
annotations=[]
for frame,points in raw.items():
 reg=registration[frame];sl=reg['slice'];second=round(int(frame)*44.36663333333333/24,3)
 digest=hashlib.sha256((root/f'evidence/video/frame-{int(frame):02}.png').read_bytes()).hexdigest()
 for i,(label,x,y) in enumerate(points):
  annotations.append({'id':f'video-v1-s{sl:02}-{i:02}','slice':sl,'label':label,'category':category(label),'x':round((x+reg['dx'])/1920,7),'y':round((y+reg['dy'])/1080,7),'note':f'Transcription manuelle de la vidéo fournie, à {second:.2f} s, coupe {sl}/24. Position reportée par recalage d’image (+70 px, +13 px). À valider ; ce repère ne constitue pas une identification médicale indépendante.','source':{'kind':'user-video','frame':int(frame),'timestamp':second,'frameSha256':digest,'pointInFrame':[x,y],'registration':[reg['dx'],reg['dy']],'status':'transcribed-not-clinically-validated'}})
payload={'version':1,'series':'24 captures IRM axiales fournies','annotations':annotations}
(root/'dist/video-annotations.js').write_text('export const videoAnnotations = '+json.dumps(annotations,ensure_ascii=False,indent=2)+';\n',encoding='utf-8')
(root/'dist/annotations-video.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'annotations':len(annotations),'slices':sorted(set(a['slice'] for a in annotations))}))
