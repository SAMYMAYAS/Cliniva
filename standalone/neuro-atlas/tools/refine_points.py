from pathlib import Path
import sys,json
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root/'tools/python'))
import cv2,numpy as np
from PIL import Image
p=root/'evidence/screenshots-geometry.json';rows=json.loads(p.read_text(encoding='utf-8'))
changes=[]
for row in rows[:24]:
 a=np.array(Image.open(Path(r'C:\Users\PROG_ONE\Pictures\Screenshots')/f'Capture d’écran ({row["file"]}).png').convert('RGB'),dtype=float)
 b=np.array(Image.open(root/f'dist/assets/slice-{row["slice"]:02}.png').convert('RGB'),dtype=float)
 aligned=np.zeros_like(a);aligned[:1067,:1850]=b[13:,70:]
 delta=np.max(abs(a-aligned),axis=2)
 cache={}
 for j,item in enumerate(row['anchors']):
  color=np.array(item['color']);key=tuple(color)
  if key not in cache:
   truecolor=np.minimum(255,np.round(color/.75));exact=np.max(abs(a-truecolor),axis=2)<2
   eroded=cv2.erode(exact.astype('uint8'),np.ones((2,2),np.uint8));_,_,stats,centers=cv2.connectedComponentsWithStats(eroded)
   dots=[]
   for stat,(px,py) in zip(stats[1:],centers[1:]):
    px-=.5;py-=.5;x,y=round(px),round(py)
    if 535<px<1260 and 80<py<950 and stat[4]>=1 and stat[2]<7 and stat[3]<7 and delta[y-2:y+3,x-2:x+3].mean()>14:dots.append((px,py))
   mask=(np.max(abs(a-color),axis=2)<65)&(delta>24)
   cache[key]=(dots,mask)
  dots,mask=cache[key];originX=498 if item['side']==0 else 1302;oy=item['y'];options=[]
  for px,py in dots:
   dist=(px-originX)*(1 if item['side']==0 else -1)
   if dist<45:continue
   xx=np.linspace(originX,px,int(dist)+1).round().astype(int);yy=np.linspace(oy,py,int(dist)+1).round().astype(int)
   hit=np.maximum.reduce([mask[np.clip(yy+offset,0,1079),xx] for offset in [-1,0,1]])
   prefix=hit[8:min(len(hit),190)].mean();whole=hit[8:-4].mean();score=prefix*.65+whole*.35+dist*.00001
   if prefix>.8 and whole>.6:options.append((score,[round(px,1),round(py,1)]))
  if options:
   point=max(options)[1]
   if item['point'] and np.linalg.norm(np.array(point)-item['point'])>5:changes.append([row['file'],j,item['point'],point])
   item['point']=point;item['dotVerified']=True
p.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(changes)
