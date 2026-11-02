from pathlib import Path
import sys,json,hashlib
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root/'tools/python'))
import cv2,numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
ocr=RapidOCR()
folder=Path(r'C:\Users\PROG_ONE\Pictures\Screenshots')
out=[]
for n in range(28,77):
 if n==52:continue
 sl=n-27 if n<52 else n-52
 a=np.array(Image.open(folder/f'Capture d’écran ({n}).png').convert('RGB'))
 texts=[]
 for side,(x0,x1) in enumerate([(232,482),(1319,1568)]):
  crop=a[65:990,x0:x1]
  gray=cv2.cvtColor(crop,cv2.COLOR_RGB2GRAY)
  # White characters on black make colored labels equally legible.
  crop=cv2.cvtColor(255-gray,cv2.COLOR_GRAY2BGR)
  result,_=ocr(cv2.resize(crop,None,fx=2,fy=2))
  for box,text,score in result or []:
   box=np.array(box)/2+[x0,65]
   texts.append(dict(side=side,label=text,score=round(score,3),box=box.tolist(),y=round(float(box[:,1].mean()),1)))
 item=dict(file=n,slice=sl,regions=n>52,texts=texts,sha256=hashlib.sha256((folder/f'Capture d’écran ({n}).png').read_bytes()).hexdigest())
 out.append(item)
 (root/'evidence/screenshots-ocr.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
 print(n,sl,len(texts),flush=True)
