import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'python'))
import av
from PIL import Image,ImageDraw
source=Path("C:/Users/PROG_ONE/Videos/Enregistrements d’écran/Enregistrement de l'écran 2026-09-12 171119.mp4")
out=Path(__file__).resolve().parent.parent/'evidence'/'video'
out.mkdir(parents=True,exist_ok=True)
container=av.open(str(source)); stream=container.streams.video[0]
duration=float(stream.duration*stream.time_base) if stream.duration else float(container.duration/av.time_base)
print(json.dumps({'duration_seconds':duration,'width':stream.width,'height':stream.height,'fps':float(stream.average_rate)}))
interval=max(1,duration/24)
frames=[]
for index in range(24):
    second=index*interval
    container.seek(int(second/stream.time_base),stream=stream)
    for frame in container.decode(stream):
        if float(frame.pts*stream.time_base)>=second:
            im=frame.to_image();im.save(out/f'frame-{index:02d}.png');frames.append((second,im));break
for part in range(2):
    sheet=Image.new('RGB',(1440,4*300),'#20252b');draw=ImageDraw.Draw(sheet)
    for j,(sec,im) in enumerate(frames[part*12:(part+1)*12]):
        im.thumbnail((480,270));x=j%3*480;y=j//3*300;sheet.paste(im,(x,y+25));draw.text((x+8,y+6),f'Frame {part*12+j:02d} / {sec:.1f} s',fill='white')
    sheet.save(out/f'contact-{part+1}.jpg')
