"""Headless, testable studio workflow: one image, one review, one offline export.

The model never stores provider credentials and never initiates any paid API call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import math
import os
from pathlib import Path

from PIL import Image, ImageOps

SUPPORTED = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.svg'}
RASTER = SUPPORTED - {'.svg'}


def _valid_bbox(box, width, height):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError('文字框必须包含左、上、右、下四个数字')
    try:
        a, b, c, d = map(float, box)
    except (ValueError, TypeError) as exc:
        raise ValueError('文字框坐标应为数字') from exc
    if not all(math.isfinite(v) for v in (a, b, c, d)):
        raise ValueError('文字框不能包含无穷值或 NaN')
    if not (0 <= a < c <= width and 0 <= b < d <= height):
        raise ValueError(f'文字框必须在 {width:g}×{height:g} 画布内，且右下角大于左上角')
    return [round(v, 3) for v in (a, b, c, d)]


def check_manifest(doc, image_size):
    if not isinstance(doc, dict):
        raise ValueError('文字清单必须是含 source_canvas、texts 的 JSON 对象')
    canvas = doc.get('source_canvas', {})
    if not isinstance(canvas, dict) or not isinstance(doc.get('texts'), list):
        raise ValueError('文字清单缺少 source_canvas 或 texts')
    w, h = image_size
    try:
        dw, dh = float(canvas['width']), float(canvas['height'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError('文字清单中的画布尺寸无效') from exc
    if abs(dw-w)>1 or abs(dh-h)>1:
        raise ValueError(f'文字清单画布 {dw:g}×{dh:g} 与当前原图 {w}×{h} 不匹配')
    seen=set()
    for i, entry in enumerate(doc['texts'], 1):
        if not isinstance(entry, dict):
            raise ValueError(f'第 {i} 条 OCR 记录不是对象')
        try:
            ident=int(entry.get('id',i))
        except (ValueError, TypeError) as exc:
            raise ValueError(f'第 {i} 条编号无效') from exc
        if ident in seen:raise ValueError(f'重复的文字编号：{ident}')
        seen.add(ident)
        if not str(entry.get('contents','')).strip():raise ValueError(f'第 {i} 条文字为空')
        _valid_bbox(entry.get('bbox'),w,h)
    return True


def local_ocr_manifest(img, ocr_data: dict, confidence=45) -> dict:
    """Convert Tesseract image_to_data output into a review-first manifest.

    Each grouped line keeps the union of actual word bounding boxes.  This is
    only a draft: local OCR boxes still require review before text cleaning.
    """
    w,h=img.size
    lines={}
    entries=ocr_data.get('text', [])
    for i,value in enumerate(entries):
        content=str(value or '').strip()
        if not content:continue
        try:
            conf=float(ocr_data['conf'][i]);x=int(ocr_data['left'][i]);y=int(ocr_data['top'][i])
            wd=int(ocr_data['width'][i]);ht=int(ocr_data['height'][i])
            group=tuple(int(ocr_data[k][i]) for k in ('page_num','block_num','par_num','line_num'))
        except (KeyError,IndexError,TypeError,ValueError):continue
        if conf < confidence or wd<=0 or ht<=0:continue
        x=max(0,min(x,w));y=max(0,min(y,h))
        right=min(w,x+wd);bottom=min(h,y+ht)
        if right<=x or bottom<=y:continue
        lines.setdefault(group,[]).append((x,y,right,bottom,content))
    out=[]
    for group,parts in sorted(lines.items(),key=lambda t:(min(p[1] for p in t[1]),min(p[0] for p in t[1]))):
        parts.sort(key=lambda p:p[0])
        x0=min(p[0] for p in parts);y0=min(p[1] for p in parts)
        x1=max(p[2] for p in parts);y1=max(p[3] for p in parts)
        wording=' '.join(p[4] for p in parts)
        out.append({'id':len(out)+1,'contents':wording,'bbox':[x0,y0,x1,y1],
                    'position':[x0,y1],'fontSize':max(6,round((y1-y0)*1.05,1)),
                    'fontFamily':'Arial','color':'#131a22','reviewed':False,'source':'tesseract'})
    return {'source_canvas':{'width':w,'height':h},'texts':out,
            'workflow':'local-ocr-review','review_required':True}


@dataclass
class StudioState:
    source: Path | None = None
    output: Path | None = None
    image_size: tuple[int,int] | None = None
    manifest: dict | None = None
    manifest_path: Path | None = None
    approved: bool = False
    dirty: bool = False
    stage: int = 0
    last_result: dict | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def has_image(self):return self.source is not None

    @property
    def has_texts(self):return bool(self.manifest and self.manifest['texts'])

    @property
    def review_count(self):return len(self.manifest['texts']) if self.manifest else 0

    def open_source(self, path: str | Path):
        path=Path(path).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            raise ValueError('请选择有效的 PNG、JPG、WebP、TIFF、BMP 或 SVG 图片')
        if self.source==path:return
        if path.suffix.lower()=='.svg':
            import xml.etree.ElementTree as ET
            # Renderer owns parsing and security, do not parse untrusted SVG here.
            from engine import _parse_svg_scene
            scene=_parse_svg_scene(path)
            size=(round(scene.width),round(scene.height))
        else:
            with Image.open(path) as im:
                im=ImageOps.exif_transpose(im)
                size=im.size
        if min(size)<=0 or max(size)>12000 or size[0]*size[1]>25_000_000:
            raise ValueError('图片尺寸过大（最大 25MP），请先裁剪或缩放')
        self.source=path;self.output=path.with_name(path.stem+'_editable.pptx')
        self.image_size=size;self.manifest=None;self.manifest_path=None
        self.approved=False;self.dirty=False;self.stage=0;self.last_result=None;self.issues=[]

    def set_manifest(self, doc:dict, path: str|Path|None=None):
        if not self.image_size:raise ValueError('请先选择原图')
        check_manifest(doc,self.image_size)
        self.manifest=copy.deepcopy(doc)
        self.approved=bool(doc.get('review_required') is False and all(t.get('reviewed') is True for t in doc['texts']))
        self.manifest_path=Path(path).resolve() if path else None
        self.dirty=False;self.stage=1
        self.last_result=None

    def load_manifest(self,path):
        path=Path(path).expanduser().resolve()
        self.set_manifest(json.loads(path.read_text(encoding='utf-8')),path)

    def _next_id(self):
        return max([int(t.get('id',0)) for t in (self.manifest or {}).get('texts',[])]+[0])+1

    def change_text(self,ident:int, contents:str, box:list[float],font_size:float|None=None):
        if not self.image_size:raise ValueError('请先导入原图')
        content=contents.strip()
        if not content:raise ValueError('文字不能为空，可以使用“删除文字”')
        clean_box=_valid_bbox(box,*self.image_size)
        f=float(font_size if font_size is not None else max(6,(clean_box[3]-clean_box[1])*1.05))
        if not math.isfinite(f) or not 3 <= f <= 200:raise ValueError('字体大小必须在 3–200 之间')
        if not self.manifest:
            w,h=self.image_size
            self.manifest={'source_canvas':{'width':w,'height':h},'texts':[],
                           'workflow':'manual-review','review_required':True}
        ident=int(ident) if ident else self._next_id()
        found=next((x for x in self.manifest['texts'] if int(x.get('id',-1))==ident),None)
        if found is None:
            found={'id':ident,'fontFamily':'Arial','color':'#131a22','source':'manual'}
            self.manifest['texts'].append(found)
        found.update({'contents':content,'bbox':clean_box,
                      'position':[clean_box[0],clean_box[3]],'fontSize':round(f,2),'reviewed':False})
        self.dirty=True;self.approved=False;self.manifest['review_required']=True
        self.last_result=None
        return ident

    def remove_text(self,ident:int):
        if not self.manifest:return
        before=len(self.manifest['texts'])
        self.manifest['texts']=[t for t in self.manifest['texts'] if int(t.get('id',-1))!=int(ident)]
        if len(self.manifest['texts'])!=before:
            self.dirty=True;self.approved=False;self.manifest['review_required']=True
            self.last_result=None

    def approve(self):
        if not self.manifest:raise ValueError('还没有可以审核的文字清单')
        check_manifest(self.manifest,self.image_size)
        for t in self.manifest['texts']:t['reviewed']=True
        self.manifest['review_required']=False
        self.approved=True;self.dirty=True

    def save_manifest(self,path:str|Path):
        if not self.manifest or not self.source:raise ValueError('没有文字清单可保存')
        path=Path(path).expanduser().resolve()
        if path==self.source:raise ValueError('不能覆盖原图')
        check_manifest(self.manifest,self.image_size)
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_name(path.name+'.tmp')
        try:
            tmp.write_text(json.dumps(self.manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(tmp,path)
        finally:
            if tmp.exists():tmp.unlink()
        self.manifest_path=path;self.dirty=False
        return path

    def review_path(self):
        if not self.source:raise ValueError('先选择原图')
        return self.source.parent/(self.source.stem+'_su7_work')/'ocr-manifest.json'

    def offline_export_options(self, *, clean=False, **extra):
        """Always offline; AI/OCR cannot accidentally run a second time."""
        if not self.source or not self.output:raise ValueError('请先选择源图和导出位置')
        if clean and (not self.approved or not self.has_texts):
            raise ValueError('去字前必须先识别并确认文字框；可关闭“局部去字”继续')
        from engine import Settings
        return Settings(ai_mode='off',ocr=False,manifest_clean=bool(clean),inpaint=bool(clean),**extra)
