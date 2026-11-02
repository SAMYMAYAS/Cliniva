from pathlib import Path
import sys,json,colorsys
root=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(root/'tools/python'))
import cv2,numpy as np
from PIL import Image
folder=Path(r'C:\Users\PROG_ONE\Pictures\Screenshots')
(root/'dist/regions').mkdir(exist_ok=True)
info=[]
for sl in range(1,25):
 a=np.array(Image.open(folder/f'Capture d’écran ({sl+52}).png').convert('RGB'))
 hsv=cv2.cvtColor(a,cv2.COLOR_RGB2HSV)
 chroma=np.ptp(a.astype(float),axis=2)
 valid=(chroma>18)&(hsv[:,:,1]>50)
 valid[:150]=False;valid[950:]=False;valid[:,:540]=False;valid[:,1255:]=False
 # Broad hue families retain the supplied contours without thin leader lines.
 groups=[(0,22,'#ed9953'),(23,37,'#c4ba45'),(38,62,'#7ecb38'),(63,81,'#35d7ba'),(82,97,'#36d9d0'),(98,102,'#60c4df'),(103,122,'#4c91e8'),(123,132,'#8d62ce'),(133,145,'#b28bd4'),(146,163,'#be6ed1')]
 paths=[]
 for lo,hi,color in groups:
  mask=(valid&(hsv[:,:,0]>=lo)&(hsv[:,:,0]<=hi)).astype('uint8')
  mask=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
  mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
  contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
  for contour in contours:
   if cv2.contourArea(contour)<250:continue
   points=cv2.approxPolyDP(contour,1.1,True).reshape(-1,2)+[70,13]
   d='M '+' L '.join(f'{x},{y}' for x,y in points)+' Z'
   paths.append(f'<path d="{d}" fill="{color}"/>')
 svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080">'+''.join(paths)+'</svg>'
 (root/f'dist/regions/slice-{sl:02}.svg').write_text(svg,encoding='utf-8')
 info.append(dict(slice=sl,contours=len(paths)))
(root/'evidence/regions-contours.json').write_text(json.dumps(info,indent=2))
print(info)
