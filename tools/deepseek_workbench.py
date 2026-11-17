#!/usr/bin/env python3
"""Auditable and cost-aware DeepSeek OCR workbench for scientific diagrams.

Two-step workflow:
    scan    -> sends 1 or 4 OCR API requests, saves MANIFEST + review overlay
    rebuild -> NO network calls, consumes corrected manifest and exports PPTX/SVG
"""
from __future__ import annotations
import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageOps
from ai_bridge import AIConfig, AIServiceError
from deepseek_workflow import (recognize,write_review_bundle,manifest_from_ocr,
                               flash_review,OCR_PRICE_PER_1000_PAGES_CNY)
from engine import Settings, convert


def load_rgb(path):
    with Image.open(path) as im:
        im=ImageOps.exif_transpose(im)
        if im.width*im.height>25_000_000:
            raise ValueError('Image too large for review; crop it first (limit 25 MP)')
        return np.asarray(im.convert('RGB'))


def main(argv=None):
    p=argparse.ArgumentParser(description='DeepSeek official OCR: review first, editable rebuild offline')
    sub=p.add_subparsers(dest='action',required=True)
    scan=sub.add_parser('scan',help='Authorize one/four DeepSeek OCR calls and save review output')
    scan.add_argument('--image',required=True,type=Path)
    scan.add_argument('--out-dir',required=True,type=Path)
    scan.add_argument('--tiling',choices=['single','2x2'],default='single')
    scan.add_argument('--purpose',choices=['text_recognition','rich_layout_recognition'],default='text_recognition')
    scan.add_argument('--flash-review',action='store_true',help='Extra paid deepseek-flash vision JSON suggestions; never auto applied')
    scan.add_argument('--model',default='deepseek-flash')
    rebuild=sub.add_parser('rebuild',help='Offline PPTX/SVG reconstruction using reviewed manifest')
    rebuild.add_argument('--image',required=True,type=Path)
    rebuild.add_argument('--manifest',required=True,type=Path)
    rebuild.add_argument('--output',required=True,type=Path)
    rebuild.add_argument('--backend',choices=['auto','contour','vtracer'],default='auto')
    rebuild.add_argument('--colors',type=int,default=24)
    rebuild.add_argument('--max-shapes',type=int,default=6000)
    rebuild.add_argument('--erase-text',action='store_true',help='Use positioned bbox in the manifest for local inpainting')
    rebuild.add_argument('--approve-review',action='store_true',help='I have manually reviewed text and location boxes')
    args=p.parse_args(argv)
    if args.action=='scan':
        if not args.image.is_file():p.error('Image not found')
        requests=4 if args.tiling=='2x2' else 1
        quoted=round(OCR_PRICE_PER_1000_PAGES_CNY*requests/1000,6)
        print(f'将上传图像到 DeepSeek 官方 OCR：预计 {requests} 次 OCR 请求；')
        print(f'参考报价约 {quoted:.4f} 元人民币（按成功页数计费，具体以 DeepSeek 当前定价为准）。')
        if args.flash_review:print('此外还将增加 1 次可能收费的 DeepSeek Flash 视觉请求。')
        print('图像将发送给 DeepSeek。发生超时后不要盲目重试，服务端可能已经计费。')
        if input('输入 YES 授权上述请求：').strip()!='YES':
            print('已取消，未调用 API。');return 0
        key=os.environ.get('DEEPSEEK_API_KEY','').strip() or getpass('DeepSeek Key (hidden): ').strip()
        if not key:raise ValueError('API Key not supplied')
        config=AIConfig(base_url='https://api.deepseek.com',vision_model=args.model,api_key=key)
        image=load_rgb(args.image)
        result=recognize(image,config,tiling=args.tiling,purpose=args.purpose)
        files=write_review_bundle(image,result,args.out_dir)
        if args.flash_review:
            report=flash_review(image,manifest_from_ocr(result,image.shape[1],image.shape[0]),config)
            target=Path(args.out_dir)/'flash-suggestions.json'
            target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            files['flash_suggestions']=str(target)
        print(json.dumps({'recognized':len(result['texts']),'requests':result['request_count'],
                           'warnings':result['warnings'],'files':files},ensure_ascii=False,indent=2))
        print('审核标注与 JSON 后再运行 rebuild；此步骤不自动重建，也不会自动擦除图像内容。')
        return 0
    if not args.image.is_file() or not args.manifest.is_file():p.error('Source or reviewed manifest missing')
    if args.erase_text and not args.approve_review:
        p.error('--erase-text requires --approve-review (verify boxes before erasing)')
    setting=Settings(colors=args.colors,backend=args.backend,max_shapes=args.max_shapes,
                     manifest_clean=args.erase_text,ai_mode='off',inpaint=True)
    svg=args.output.with_suffix('.svg') if args.output.suffix.lower()=='.pptx' else None
    stats=convert(args.image,args.output,setting,manifest_path=args.manifest,extra_svg=svg)
    print(json.dumps({'output':str(args.output),'texts':stats['texts'],'shapes':stats['shapes'],
                      'backend':stats['backend'],'warnings':stats.get('warnings',[])},ensure_ascii=False,indent=2))
    print('Offline rebuild complete: no DeepSeek API request was made.')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (AIServiceError,ValueError,RuntimeError) as e:
        print('ERROR:',str(e),file=sys.stderr)
        raise SystemExit(2)
