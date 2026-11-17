#!/usr/bin/env python3
"""Compare equal-size external PowerPoint renders against the same source.

Run *after* rendering both PPTX documents with the same renderer/settings.
This script does not call AI or network and does not claim biological semantics.
"""
import argparse, json, math
from pathlib import Path
import cv2, numpy as np
from PIL import Image,ImageDraw,ImageFont


def compare(source,target, size=(1000,545)):
    a=np.asarray(Image.open(source).convert('RGB').resize(size,Image.Resampling.LANCZOS),dtype=np.float32)
    b=np.asarray(Image.open(target).convert('RGB').resize(size,Image.Resampling.LANCZOS),dtype=np.float32)
    diff=np.abs(a-b)
    ay=cv2.cvtColor(a.astype(np.uint8),cv2.COLOR_RGB2GRAY).astype(np.float32)
    by=cv2.cvtColor(b.astype(np.uint8),cv2.COLOR_RGB2GRAY).astype(np.float32)
    m1=cv2.GaussianBlur(ay,(11,11),1.5);m2=cv2.GaussianBlur(by,(11,11),1.5)
    var1=cv2.GaussianBlur(ay*ay,(11,11),1.5)-m1*m1
    var2=cv2.GaussianBlur(by*by,(11,11),1.5)-m2*m2
    cov=cv2.GaussianBlur(ay*by,(11,11),1.5)-m1*m2
    c1=(.01*255)**2;c2=(.03*255)**2
    ssim=float(np.mean((2*m1*m2+c1)*(2*cov+c2)/((m1*m1+m2*m2+c1)*(var1+var2+c2))))
    def lab(u):
        z=cv2.cvtColor(u.astype(np.uint8),cv2.COLOR_RGB2LAB).astype(np.float32)
        z[:,:,0]*=100/255;z[:,:,1:]-=128
        return z
    de=np.linalg.norm(lab(a)-lab(b),axis=2)
    return {'ssim':round(ssim,4),'mae':round(float(diff.mean()),3),
            'mean_delta_e76':round(float(de.mean()),3),
            'p95_delta_e76':round(float(np.percentile(de,95)),3),
            'target_pixels':list(size)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    results={'renderer':'LibreOffice -> PDF -> pdftoppm 120 dpi',
             'basis':'Source/before/after all resized to 1000x545',
             'before':compare(args.source,args.before),'after':compare(args.source,args.after)}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'comparison_metrics.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    w,h=960,524
    pics=[Image.open(x).convert('RGB').resize((w,h),Image.Resampling.LANCZOS) for x in
          (args.source,args.before,args.after)]
    canvas=Image.new('RGB',(w*3,h+80),'#ffffff');d=ImageDraw.Draw(canvas)
    labels=['Original PNG','v4.1 PPTX / LibreOffice','v4.2 PPTX / LibreOffice']
    for i,(im,label) in enumerate(zip(pics,labels)):
        canvas.paste(im,(i*w,64));d.text((i*w+18,23),label,fill='#172337')
    canvas.save(args.output/'same_renderer_comparison.png')
    print(json.dumps(results,indent=2))

if __name__=='__main__':main()
