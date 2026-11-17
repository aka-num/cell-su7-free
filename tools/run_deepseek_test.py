#!/usr/bin/env python3
"""Interactive one-image DeepSeek API test. Never saves or prints your API key.

Usage: python tools/run_deepseek_test.py --image examples/demo_scientific.png
"""
import argparse
from getpass import getpass
import json
import importlib.util
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from ai_bridge import AIConfig, AIServiceError
from engine import Settings, convert


def main():
    p=argparse.ArgumentParser(description='One authorized DeepSeek image reconstruction test')
    p.add_argument('--image',type=Path,default=PROJECT/'examples'/'demo_scientific.png')
    p.add_argument('--mode',choices=['ocr','vision','layout'],default='ocr',
                   help='ocr: dedicated DeepSeek OCR; vision: Flash OCR; layout: Flash semantic shapes')
    p.add_argument('--output-dir',type=Path,default=PROJECT/'runs'/'deepseek-test')
    args=p.parse_args()
    if not args.image.is_file():p.error('Source image not found')
    print('This will upload the image to the official DeepSeek API and may incur charges.')
    if input('Type YES to authorize one real API request: ').strip()!='YES':
        print('Canceled before sending image.');return 0
    key=os.environ.get('CELL_SU7_API_KEY','').strip() or getpass('DeepSeek API Key (hidden, not stored): ').strip()
    if not key:print('No key provided.');return 2
    cfg=AIConfig(base_url='https://api.deepseek.com',vision_model='deepseek-flash',api_key=key)
    mode={'ocr':'deepseek-ocr','vision':'ocr','layout':'layout'}[args.mode]
    output_dir=args.output_dir.resolve();output_dir.mkdir(parents=True,exist_ok=True)
    stem='official-'+args.mode
    settings=Settings(ai_mode=mode, ai_config=cfg,backend='contour',colors=24,max_shapes=6000)
    report = output_dir/f'{stem}.quality.html' if importlib.util.find_spec('cairosvg') else None
    if report is None:
        print('CairoSVG unavailable: skipping optional quality HTML report.')
    try:
        result=convert(args.image,output_dir/f'{stem}.pptx',settings,
                       extra_svg=output_dir/f'{stem}.svg',
                       manifest_output=output_dir/f'{stem}.texts.json',
                       report_path=report)
    except (AIServiceError,ValueError,RuntimeError) as e:
        print('Test failed:',str(e));return 1
    finally:
        key=None  # credentials are never placed into the report or saved profile
    print('Completed:',json.dumps({k:result.get(k) for k in ('output','backend','shapes','texts','quality','ai')},ensure_ascii=False,indent=2))
    print('Review generated PPTX and SVG; do not equate SSIM with semantic accuracy.')
    return 0

if __name__=='__main__':raise SystemExit(main())
