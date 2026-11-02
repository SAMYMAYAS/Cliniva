from pathlib import Path
import json
import numpy as np
from PIL import Image
root=Path(__file__).resolve().parent.parent
pairs={4:2,3:3,5:5,6:8,2:13,0:14,1:15,12:18,11:19,8:20,9:24}
results={}
for frame,sl in pairs.items():
 a=np.asarray(Image.open(root/f'evidence/video/frame-{frame:02}.png').convert('RGB'),dtype=float)
 b=np.asarray(Image.open(root/f'dist/assets/slice-{sl:02}.png').convert('RGB'),dtype=float)
 y,x=np.mgrid[160:900:4,610:1190:4]
 av=a[y,x].mean(axis=2); mask=(a[y,x].max(axis=2)-a[y,x].min(axis=2)<10)&(av>30)&(av<230)
 best=None
 for dy in range(0,22):
  for dx in range(58,82):
   bv=b[y+dy,x+dx].mean(axis=2)
   err=float(np.median(np.abs(av[mask]-bv[mask])))
   if best is None or err<best[0]:best=(err,dx,dy)
 results[str(frame)]={'slice':sl,'dx':best[1],'dy':best[2],'median_absolute_gray_error':best[0]}
print(json.dumps(results,ensure_ascii=False,indent=2))
(root/'evidence/video/registration.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
