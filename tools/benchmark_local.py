#!/usr/bin/env python3
"""Reproducible offline before/after vector quality benchmark.

No external API, no billed requests. Reports *SVG* visual scores and native
object counts; do not mistake SVG scores for actual PowerPoint render scores.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Settings, convert


def main():
    p=argparse.ArgumentParser(description='Offline vectorization benchmark (no external API)')
    p.add_argument('--image',type=Path,required=True)
    p.add_argument('--out-dir',type=Path,required=True)
    p.add_argument('--colors',type=int,default=16)
    p.add_argument('--max-shapes',type=int,default=4000)
    p.add_argument('--precision-tile',type=int,default=192)
    p.add_argument('--separate-regions',action='store_true')
    args=p.parse_args()
    out=args.out_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    results={}
    for backend in ('contour','precision'):
        stem=f'{backend}_{args.colors}c'
        result=convert(args.image,out/(stem+'.svg'),Settings(backend=backend,colors=args.colors,
              max_shapes=args.max_shapes,precision_tile=args.precision_tile,
              precision_separate_regions=args.separate_regions),
              report_path=out/(stem+'.html'))
        results[backend]={'shapes':result['shapes'],'quality':result['quality'],
                          'svg':result['output'],'report':result['report']}
    summary={'source':str(args.image.resolve()),'colors':args.colors,'comparison_basis':'both SVG renders at same canvas/size',
             'results':results,'ssim_gain':round(results['precision']['quality']['ssim']-
                                                 results['contour']['quality']['ssim'],4)}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
