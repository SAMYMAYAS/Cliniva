from pathlib import Path
import sys,json
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root/'tools/python'))
import cv2,numpy as np
from PIL import Image
folder=Path(r'C:\Users\PROG_ONE\Pictures\Screenshots')
results=[]
for n in range(28,77):
 if n==52:continue
 sl=n-27 if n<52 else n-52
 a=np.array(Image.open(folder/f'Capture d’écran ({n}).png').convert('RGB'))
 b=np.array(Image.open(root/f'dist/assets/slice-{sl:02}.png').convert('RGB'))
 if n<52:
  y,x=np.mgrid[160:900:5,610:1190:5];av=a[y,x].mean(2);mask=(np.ptp(a[y,x],axis=2)<10)&(av>30)&(av<230)
  best=min((float(np.median(abs(av[mask]-b[y+dy,x+dx].mean(2)[mask]))),dx,dy) for dy in range(8,18) for dx in range(65,76))
  reg=(best[1],best[2]);print('registration',sl,best,flush=True)
 else:reg=tuple(results[sl-1]['registration'])
 aligned=np.zeros_like(a);aligned[:1080-reg[1],:1920-reg[0]]=b[reg[1]:,reg[0]:]
 delta=np.max(abs(a.astype(float)-aligned),axis=2)
 anchors=[]
 for side,xcol,originX in [(0,489,498),(1,1309,1302)]:
  on=np.max(a[65:990,xcol],axis=1)>70
  ys=np.where(on)[0]+65
  runs=np.split(ys,np.where(np.diff(ys)>1)[0]+1)
  for run in runs:
   if not len(run):continue
   oy=float(run.mean());color=np.median(a[run,xcol],axis=0)
   mask=(np.max(abs(a.astype(float)-color),axis=2)<65)&(delta>24)
   mask[:65]=False;mask[990:]=False;mask[:,:497]=False;mask[:,1304:]=False
   lines=cv2.HoughLinesP(mask.astype('uint8')*255,1,np.pi/1800,threshold=25,minLineLength=35,maxLineGap=9)
   candidates=[]
   for line in (lines.reshape(-1,4) if lines is not None else []):
    x1,y1,x2,y2=map(float,line)
    if x2<x1:x1,y1,x2,y2=x2,y2,x1,y1
    if x2-x1<15:continue
    slope=(y2-y1)/(x2-x1);err=abs(y1+(originX-x1)*slope-oy)
    distance=(x1-originX if side==0 else originX-x2)
    if err<3 and abs(distance)<25:candidates.append((np.hypot(x2-x1,y2-y1)-err*12-abs(distance),slope))
   endpoint=None
   if candidates:
    slope=max(candidates)[1];direction=1 if side==0 else -1;last=0;gap=0
    for step in range(1,810):
     xx=int(round(originX+direction*step));yy=int(round(oy+direction*step*slope))
     if yy<66 or yy>988 or xx<497 or xx>1303:break
     if mask[max(0,yy-2):yy+3,max(0,xx-1):xx+2].any():last=step;gap=0
     else:gap+=1
     if gap>8:break
    if last>30:endpoint=[round(originX+direction*last),round(oy+direction*last*slope)]
    truecolor=np.minimum(255,np.round(color/0.75))
    exact=np.max(abs(a.astype(float)-truecolor),axis=2)<2
    dots=cv2.erode(exact.astype('uint8'),np.ones((2,2),np.uint8))
    count,labels,stats,centroids=cv2.connectedComponentsWithStats(dots)
    dotcandidates=[]
    for stat,(px,py) in zip(stats[1:],centroids[1:]):
     px-=0.5;py-=0.5
     dist=direction*(px-originX)
     err=abs(py-(oy+(px-originX)*slope))
     if 535<px<1260 and 80<py<950 and stat[4]>=2 and stat[2]<7 and stat[3]<7 and dist>45 and err<3:
      dotcandidates.append((dist-err*10,[round(px,1),round(py,1)]))
    if dotcandidates:endpoint=max(dotcandidates)[1]
   truecolor=np.minimum(255,np.round(color/0.75))
   exact=np.max(abs(a.astype(float)-truecolor),axis=2)<2
   dots=cv2.erode(exact.astype('uint8'),np.ones((2,2),np.uint8))
   count,labels,stats,centroids=cv2.connectedComponentsWithStats(dots)
   options=[]
   for stat,(px,py) in zip(stats[1:],centroids[1:]):
    px-=0.5;py-=0.5;distance=(px-originX)*(1 if side==0 else -1)
    if not (535<px<1260 and 80<py<950 and stat[4]>=2 and stat[2]<7 and stat[3]<7 and distance>45):continue
    xx=np.linspace(originX,px,int(distance)+1).round().astype(int)
    yy=np.linspace(oy,py,int(distance)+1).round().astype(int)
    hit=np.maximum.reduce([mask[np.clip(yy+offset,0,1079),xx] for offset in [-1,0,1]])
    prefix=hit[8:min(len(hit),190)].mean();whole=hit[8:-4].mean()
    score=prefix*.65+whole*.35
    if prefix>.8 and whole>.6:options.append((score,[round(px,1),round(py,1)]))
   if options:endpoint=max(options)[1]
   anchors.append(dict(side=side,y=oy,color=color.astype(int).tolist(),point=endpoint,dotVerified=bool(options)))
 results.append(dict(file=n,slice=sl,registration=reg,anchors=anchors))
 (root/'evidence/screenshots-geometry.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
 print('points',n,len(anchors),sum(a['point'] is not None for a in anchors),flush=True)

