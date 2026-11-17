"""DeepSeek-specific OCR and scientific-figure review pipeline.

Single-page, opt-in uploads. OCR -> review artifacts -> manual correction ->
local editable reconstruction. No API key ever stored in results.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ai_bridge import (AIConfig, AIServiceError, _credentials, _session, _read_response,
                       _image_data_url, _extract_json, deepseek_ocr_image)


OCR_PRICE_PER_1000_PAGES_CNY = 1.5  # Official guidance on 2026-11-16; not a guarantee


def tile_regions(w:int,h:int,mode:str):
    if mode=='single':return [(0,0,w,h)]
    if mode!='2x2':raise ValueError('Supported modes: single, 2x2')
    # 15% overlap on each tile helps avoid splitting scientific labels.
    bx=w//2;by=h//2
    ox=max(8,round(w*.075));oy=max(8,round(h*.075))
    return [(0,0,min(w,bx+ox),min(h,by+oy)),
            (max(0,bx-ox),0,w,min(h,by+oy)),
            (0,max(0,by-oy),min(w,bx+ox),h),
            (max(0,bx-ox),max(0,by-oy),w,h)]


def _overlap_iou(a,b):
    x0=max(a[0],b[0]);y0=max(a[1],b[1]);x1=min(a[2],b[2]);y1=min(a[3],b[3])
    inter=max(0,x1-x0)*max(0,y1-y0)
    if not inter:return 0.0
    aa=(a[2]-a[0])*(a[3]-a[1]);ab=(b[2]-b[0])*(b[3]-b[1])
    return inter/max(1,aa+ab-inter)


def _overlap_over_smaller(a,b):
    x0=max(a[0],b[0]);y0=max(a[1],b[1]);x1=min(a[2],b[2]);y1=min(a[3],b[3])
    inter=max(0,x1-x0)*max(0,y1-y0)
    return inter/max(1.,min((a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1])))


def _deduplicate(items):
    ordered=sorted(items,key=lambda t:(t['bbox'][1],t['bbox'][0],-t.get('tile_id',0)))
    kept=[];duplicates=0
    for candidate in ordered:
        # Keep distinct overlapping words (NLRP3 / NLRP6); dedupe only same text.
        txt=' '.join(candidate['text'].lower().split())
        found=False
        for other in kept:
            if ' '.join(other['text'].lower().split())!=txt:continue
            if _overlap_iou(candidate['bbox'],other['bbox'])>.28 or _overlap_over_smaller(candidate['bbox'],other['bbox'])>.65:
                found=True;duplicates+=1;break
        if not found:kept.append(candidate)
    return kept,duplicates


def recognize(img:np.ndarray, config:AIConfig, *, tiling:str='single',
              purpose:str='text_recognition', recognizer=None) -> dict:
    """One or four explicitly selected OCR requests. No automatic retries.

    Returned normalized coordinates are always in the ORIGINAL image frame.
    Cost estimate must be displayed before initiating this function.
    """
    if img.ndim!=3 or img.shape[2]!=3:raise ValueError('Input image must be RGB')
    h,w=img.shape[:2]
    if not 1<=w<=12000 or not 1<=h<=12000 or w*h>25_000_000:
        raise ValueError('Image resolution outside the supported review workflow limits')
    regions=tile_regions(w,h,tiling)
    run=recognizer or deepseek_ocr_image
    all_items=[];warnings=[];usage=[];unpositioned=0;partial_tiles=[]
    for idx,(x0,y0,x1,y1) in enumerate(regions):
        patch=img[y0:y1,x0:x1]
        # Only upscale crops in tiled mode; smaller labels benefit from more pixels.
        # DeepSeek OCR has a 100-MiB upload cap; cap the largest edge at 2400.
        if tiling!='single':
            im=Image.fromarray(patch)
            factor=min(2.0,2400/max(im.size))
            if factor>1.05:im=im.resize((round(im.width*factor),round(im.height*factor)),Image.Resampling.LANCZOS)
            patch=np.asarray(im.convert('RGB'))
        parsed=run(patch,config,purpose=purpose)
        usage.append(parsed.get('usage',{}))
        if parsed.get('partial') or parsed.get('finish_reason')=='length':partial_tiles.append(idx+1)
        unpositioned+=parsed.get('unpositioned',0)
        warnings.extend(f'ROI {idx+1}: {wmsg}' for wmsg in parsed.get('warnings',[]))
        for label in parsed.get('texts',[]):
            lx,ty,rx,by=label['bbox']
            absbox=[x0+(lx/1000)*(x1-x0),y0+(ty/1000)*(y1-y0),
                    x0+(rx/1000)*(x1-x0),y0+(by/1000)*(y1-y0)]
            norm=[round(absbox[0]/w*1000,4),round(absbox[1]/h*1000,4),
                  round(absbox[2]/w*1000,4),round(absbox[3]/h*1000,4)]
            all_items.append({'text':label['text'],'bbox':norm,'tile_id':idx})
    distinct,dups=_deduplicate(all_items)
    if dups:warnings.append(f'{dups} overlapping OCR duplicate(s) removed across tiles')
    if not distinct:warnings.append('No positioned text recognized; review before conversion')
    if partial_tiles:warnings.append('OCR 结果不完整：第 '+','.join(map(str,partial_tiles))+' 区达到输出长度上限；已保留有效文字框。可直接校对，或明确授权重新分区识别。')
    return {'texts':distinct,'shapes':[],'model':'deepseek-ocr','tile_mode':tiling,
            'request_count':len(regions),'page_count':len(regions),'unpositioned':unpositioned,
            'estimated_ocr_cost_cny':round(OCR_PRICE_PER_1000_PAGES_CNY*len(regions)/1000,6),
            'warnings':warnings,'usage':usage,'partial':bool(partial_tiles),
            'partial_tiles':partial_tiles,'needs_review':True}


def manifest_from_ocr(data:dict,w:int,h:int) -> dict:
    texts=[]
    for n,t in enumerate(data['texts'],1):
        x0,y0,x1,y1=t['bbox']
        px=[round(x0*w/1000,2),round(y0*h/1000,2),round(x1*w/1000,2),round(y1*h/1000,2)]
        size=max(6.,round((px[3]-px[1])*1.05,2))
        texts.append({'id':n,'contents':t['text'],'position':[px[0],px[3]],
                      'fontSize':size,'fontFamily':'Arial','color':'#131a22',
                      'bbox':px,'source':'deepseek-ocr','reviewed':False})
    return {'source_canvas':{'width':w,'height':h},'texts':texts,
            'workflow':'deepseek-ocr-review','review_required':True}


def _font(size=13):
    candidates=['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                'C:/Windows/Fonts/arial.ttf','/System/Library/Fonts/Supplemental/Arial.ttf']
    for path in candidates:
        try:return ImageFont.truetype(path,size)
        except OSError:continue
    return ImageFont.load_default()


def render_review(img:np.ndarray, manifest:dict) -> Image.Image:
    base=Image.fromarray(img).convert('RGBA')
    overlay=Image.new('RGBA',base.size,(0,0,0,0));d=ImageDraw.Draw(overlay)
    w,h=base.size
    font=_font(13)
    for item in manifest['texts']:
        x0,y0,x1,y1=item['bbox']
        xy=[round(x0),round(y0),round(x1),round(y1)]
        if xy[2]<=xy[0] or xy[3]<=xy[1]:continue
        d.rectangle(xy,outline=(250,30,75,230),width=2,fill=(247,40,92,24))
        label=str(item['id'])
        px=max(0,min(w-20,int(x0)));py=max(0,int(y0)-17)
        d.rounded_rectangle((px,py,px+max(19,9*len(label)+7),py+16),radius=3,fill=(38,39,54,230))
        d.text((px+4,py),label,font=font,fill=(255,255,255,255))
    return Image.alpha_composite(base,overlay).convert('RGB')


def write_review_bundle(img:np.ndarray, recognition:dict, dest:Path) -> dict:
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    h,w=img.shape[:2]
    manifest=manifest_from_ocr(recognition,w,h)
    # No secrets or raw headers ever persisted; only recognized text and diagnostics.
    outputs={'manifest':dest/'ocr-manifest.json','overlay':dest/'ocr-review.png',
             'diagnostics':dest/'ocr-diagnostics.json','html':dest/'ocr-review.html'}
    outputs['manifest'].write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    render_review(img,manifest).save(outputs['overlay'])
    diag={k:recognition.get(k) for k in ('tile_mode','request_count','page_count','estimated_ocr_cost_cny','warnings','unpositioned','partial','partial_tiles')}
    diag.update({'recognized_boxes':len(manifest['texts']),'status':'REVIEW_REQUIRED'})
    outputs['diagnostics'].write_text(json.dumps(diag,ensure_ascii=False,indent=2),encoding='utf-8')
    # HTML shows review numbering without inserting untrusted OCR labels as raw HTML.
    from html import escape
    rows='\\n'.join(f'<tr><td>{int(t["id"])}</td><td>{escape(t["contents"])}</td><td>{escape(str(t["bbox"]))}</td><td>待核对</td></tr>' for t in manifest['texts'])
    page=('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><title>DeepSeek OCR 审核</title>'
          '<style>body{font:15px system-ui;background:#111b2a;color:#e9edf5;margin:24px}img{max-width:100%}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #344255;padding:8px;text-align:left}</style>'
          '</head><body><h1>DeepSeek OCR 人工审核</h1><p>红框是模型给出的文本位置。请在 ocr-manifest.json 中更正文字或坐标；未核对前不要自动去字。</p>'
          '<img alt="OCR bounding boxes" src="ocr-review.png"><table><tr><th>#</th><th>文字</th><th>原图像素框</th><th>审核</th></tr>'+rows+'</table></body></html>')
    outputs['html'].write_text(page,encoding='utf-8')
    return {k:str(v) for k,v in outputs.items()}


def flash_review(img:np.ndarray, manifest:dict, config:AIConfig) -> dict:
    """Optional ONE additional paid vision call. Suggestions only: NEVER mutate manifest.

    The official JSON Output interface is used; no invented bounding boxes.
    """
    labels=[{'id':x['id'],'text':x['contents']} for x in manifest['texts'][:120]]
    prompt=('Read the SCIENTIFIC image. Compare only the following OCR guesses with the visible labels. '
            'Produce JSON only, shaped as {"suggestions":[{"id":1,"text":"correct visible text","reason":"short"}]}. '
            'Do not add new objects or new coordinates. Only return corrections that are VERY CLEAR. '
            'If no corrections are certain, return {"suggestions":[]}. Current OCR JSON: '+json.dumps(labels,ensure_ascii=False))
    body={'model':config.vision_model,'messages':[{'role':'user','content':[
          {'type':'text','text':prompt},
          {'type':'image_url','image_url':{'url':_image_data_url(img,max_edge=2400),'detail':'original'}}]}],
          'response_format':{'type':'json_object'},'thinking':{'type':'disabled'},'max_tokens':4000,'stream':False}
    headers=_credentials(config);headers['Content-Type']='application/json'
    try:
        with _session() as sess:
            resp=sess.post(config.base_url.rstrip('/')+'/chat/completions',json=body,headers=headers,
                           timeout=(15,max(90,config.timeout)),allow_redirects=False)
        if 300<=resp.status_code<400:raise AIServiceError('Redirect blocked to protect API key')
        result=_read_response(resp)
        choice=result['choices'][0]
        if choice.get('finish_reason')=='length':raise AIServiceError('DeepSeek Flash review JSON truncated')
        raw=choice['message']['content']
        if not isinstance(raw,str) or not raw.strip():raise AIServiceError('DeepSeek Flash returned empty JSON')
        data=_extract_json(raw)
        allowed={t['id']:t['contents'] for t in manifest['texts']}
        proposals=[]
        for v in data.get('suggestions',[])[:120]:
            if not isinstance(v,dict):continue
            i=v.get('id');new=v.get('text')
            if type(i)!=int or i not in allowed or not isinstance(new,str) or not new.strip() or len(new)>300:continue
            if new.strip()!=allowed[i]:proposals.append({'id':i,'original':allowed[i], 'suggestion':new.strip()})
        return {'suggestions':proposals,'model':config.vision_model,
                'warning':'Suggestions are unverified and are NEVER auto-applied.'}
    except AIServiceError:raise
    except (ValueError,IndexError,KeyError,TypeError) as e:
        raise AIServiceError('Malformed DeepSeek Flash review response') from e
    except Exception as e:
        raise AIServiceError('DeepSeek Flash review request failed or timed out; it may have been billed') from e
