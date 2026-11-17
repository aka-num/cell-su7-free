#!/usr/bin/env python3
"""Make a truthful 2x2 comparison: source vs SVG and actual LibreOffice PPT renders."""
from pathlib import Path
import io,json,sys,math
import numpy as np,cv2
from PIL import Image,ImageDraw,ImageFont
import cairosvg
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'release_artifacts'
S=(820,447)
source_full=Image.open(ROOT/'examples/demo_scientific.png').convert('RGB')
source=source_full.resize(S,Image.Resampling.LANCZOS)
old=Image.open(io.BytesIO(cairosvg.svg2png(url=str(ROOT/'examples/pathogen_precision16.svg'),output_width=S[0],output_height=S[1]))).convert('RGB')
color_full=Image.open(WORK/'render/color_first_page.png').convert('RGB')
color=color_full.resize(S,Image.Resampling.LANCZOS)
shape_full=Image.open(WORK/'render/page.png').convert('RGB')
shape=shape_full.resize(S,Image.Resampling.LANCZOS)
panels=[source,old,color,shape]
names=['Original scientific figure','v4.0 / 16-color SVG','v4.1 / color-first PPTX','v4.1 / structure-first PPTX']
base=Image.new('RGB',(820*2+48,447*2+168),(18,26,40));d=ImageDraw.Draw(base)
try:
 f=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',21)
 title=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28)
except Exception:f=ImageFont.load_default();title=f
# Each panel is derived from a real original file or exported vector file.
d.text((24,10),'Cell SU7 v4.1 / same-image visual comparison',fill=(238,246,255),font=title)
for i,(image,label) in enumerate(zip(panels,names)):
 col=i%2;row=i//2;x=16+col*(820+16);y=85+row*(447+38)
 d.text((x,y-29),label,fill=(190,211,236),font=f)
 base.paste(image,(x,y))
base.save(WORK/'comparison_v4_v4_1.png',optimize=True)
# Standard-size independent PPT render evaluation; not the SVG scores.
def metrics(image):
 a=np.asarray(source.resize((1000,545),Image.Resampling.LANCZOS),dtype=np.uint8)
 b=np.asarray(image.resize((1000,545),Image.Resampling.LANCZOS),dtype=np.uint8)
 aa=a.astype(np.float32);bb=b.astype(np.float32)
 mae=float(np.mean(abs(aa-bb)))
 x=cv2.cvtColor(a,cv2.COLOR_RGB2GRAY).astype(np.float32)
 y=cv2.cvtColor(b,cv2.COLOR_RGB2GRAY).astype(np.float32)
 mx=cv2.GaussianBlur(x,(11,11),1.5);my=cv2.GaussianBlur(y,(11,11),1.5)
 vx=cv2.GaussianBlur(x*x,(11,11),1.5)-mx*mx
 vy=cv2.GaussianBlur(y*y,(11,11),1.5)-my*my
 cov=cv2.GaussianBlur(x*y,(11,11),1.5)-mx*my
 c1=(.01*255)**2;c2=(.03*255)**2
 ssim=float(np.mean((2*mx*my+c1)*(2*cov+c2)/((mx*mx+my*my+c1)*(vx+vy+c2))))
 def lab(img):
  q=cv2.cvtColor(img,cv2.COLOR_RGB2LAB).astype(np.float32)
  q[...,0]*=100/255;q[...,1:]-=128
  return q
 delta=np.linalg.norm(lab(a)-lab(b),axis=2)
 return {'ssim':round(ssim,4),'mae':round(mae,3),'delta_e76':round(float(delta.mean()),3)}
print('actual PPTX render metrics via LibreOffice',metrics(color_full),metrics(shape_full))
j=json.loads((WORK/'metrics_summary.json').read_text());j['v4.1_color32_PPTX_LO']=metrics(color_full);j['v4.1_structure16_PPTX_LO']=metrics(shape_full)
(WORK/'metrics_summary.json').write_text(json.dumps(j,indent=2,ensure_ascii=False))
