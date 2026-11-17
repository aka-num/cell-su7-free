"""Editable scientific-figure reconstruction engine.
Local-first, with explicit optional external multimodal AI providers.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from collections import Counter
import io, json, math, re, tempfile, importlib.util, warnings
import xml.etree.ElementTree as ET
import cv2
import numpy as np
from PIL import Image, ImageOps
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml import parse_xml
from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import nsdecls
from pptx.util import Inches, Pt
from defusedxml import ElementTree as SafeET
from geometry import Path as VectorPath, polygon, ellipse, parse_path, transform_parse, mul, IDENT, path_bbox, apply_matrix, NUM, normalize_evenodd

@dataclass
class Settings:
    colors:int=24
    min_area:int=18
    simplify:float=.0015
    max_pixels:int=3_000_000
    max_shapes:int=4000
    slide_width:float=12.0
    ocr:bool=False
    ocr_lang:str='eng'
    ocr_confidence:int=40
    inpaint:bool=True
    inpaint_radius:int=3
    backend:str="auto"  # auto / precision / contour / vtracer; precision is local pixel-edge tracing
    precision_tile:int=192
    precision_max_commands:int=2_000_000
    precision_separate_regions:bool=False
    precision_min_region_area:int=64
    color_mode:str="perceptual"       # legacy / balanced / perceptual / fusion
    luminance_weight:float=1.7
    background_gradient:bool=True
    structure_mode:str="hybrid"        # off / annotate / hybrid / objects
    ocr_layout:str="line"  # line / word
    group_regions:bool=False
    ai_mode:str='off'            # off / ocr / hybrid / layout / deepseek-ocr; never contact providers without an explicit opt-in
    ai_config:object|None=None    # ai_bridge.AIConfig
    ai_remote_clean:bool=False    # use image-edit endpoint AFTER obtaining an OCR mask
    ai_accept_changes:bool=False  # accept potentially changed geometry outside mask
    clean_preview:str|None=None   # optional output path for cleaning review
    deepseek_tiling:str='single'   # single / 2x2; 2x2 triggers FOUR billable OCR requests
    deepseek_purpose:str='text_recognition' # or rich_layout_recognition
    manifest_clean:bool=False      # explicitly reviewed, positioned manifest bounding boxes ONLY

@dataclass
class Shape:
    path:VectorPath
    fill:tuple[int,int,int]|None=(0,0,0)
    stroke:tuple[int,int,int]|None=None
    stroke_width:float=1.0
    opacity:float=1.0
    name:str='shape'
    fill_rule:str='nonzero'
    stroke_opacity:float=1.0
    gradient:dict|None=None
    dasharray:tuple[float,...]|None=None
    stroke_gradient:dict|None=None
    primitive:str|None=None  # Native editable geometry: ellipse / rect / line.

@dataclass
class Text:
    content:str
    x:float
    y:float
    size:float
    color:tuple[int,int,int]=(0,0,0)
    family:str='Arial'
    angle:float=0.0
    anchor:str='start'
    opacity:float=1.0

@dataclass
class Scene:
    width:float
    height:float
    shapes:list[Shape]
    texts:list[Text]
    # Preserve SVG painter order, including interleaved text and paths.
    paint_order:list[tuple[str,int]]=field(default_factory=list)
    warnings:list[str]=field(default_factory=list)

    def add_shape(self, shape):
        self.paint_order.append(('shape',len(self.shapes)))
        self.shapes.append(shape)

    def add_text(self, text):
        self.paint_order.append(('text',len(self.texts)))
        self.texts.append(text)

    def order(self):
        if not self.paint_order:
            return [('shape',i) for i in range(len(self.shapes))]+[('text',i) for i in range(len(self.texts))]
        seen=set(self.paint_order)
        before=[('shape',i) for i in range(len(self.shapes)) if ('shape',i) not in seen]
        after=[('text',i) for i in range(len(self.texts)) if ('text',i) not in seen]
        return before+self.paint_order+after

def check_settings(s):
    if not 2<=s.colors<=64:raise ValueError('colors must be between 2 and 64')
    if not 0<=s.min_area<=100000:raise ValueError('min_area out of range')
    if not 0<=s.simplify<=.1:raise ValueError('simplify out of range')
    if s.max_pixels<=0 or s.max_shapes<=0 or s.slide_width<=0:raise ValueError('invalid limits')
    if s.backend not in ('auto','precision','contour','vtracer'):raise ValueError('backend must be auto, precision, contour or vtracer')
    if s.precision_tile<32 or s.precision_max_commands<10 or s.precision_min_region_area<2:raise ValueError('Invalid precision tracing limits')
    if s.color_mode not in ('legacy','balanced','perceptual','fusion'):raise ValueError('color_mode must be legacy, balanced, perceptual or fusion')
    if s.structure_mode not in ('off','annotate','hybrid','objects'):raise ValueError('structure_mode must be off, annotate, hybrid or objects')
    if not .5 <= s.luminance_weight <= 4:raise ValueError('luminance_weight must be 0.5..4')
    if s.ocr_layout not in ('line','word'):raise ValueError('ocr_layout must be line or word')
    if s.ai_mode not in ('off','ocr','hybrid','layout','deepseek-ocr'):raise ValueError('ai_mode must be off, ocr, hybrid, layout, or deepseek-ocr')
    if s.deepseek_tiling not in ('single','2x2'):raise ValueError('deepseek_tiling must be single or 2x2')
    if s.deepseek_purpose not in ('text_recognition','rich_layout_recognition'):raise ValueError('Invalid DeepSeek OCR purpose')
    if s.manifest_clean and not s.inpaint:raise ValueError('Manifest cleaning requires inpainting')
    if s.manifest_clean and (s.ai_mode!='off' or s.ocr):raise ValueError('Only clean reviewed manifest in offline mode')
    if s.ai_mode!='off' and s.ocr:raise ValueError('Choose local OCR or AI OCR, not both (hybrid mode runs local OCR automatically)')
    if s.ai_remote_clean and s.ai_config and getattr(s.ai_config, 'base_url', '').rstrip('/').startswith('https://api.deepseek.com'):
        raise ValueError('DeepSeek official API does not provide the /images/edits interface; use local inpainting')
    if s.ai_remote_clean and s.ai_mode=='layout':raise ValueError('AI editing is unavailable in layout-only mode')
    if s.ai_remote_clean and not s.inpaint:raise ValueError('AI editing requires text cleaning to be enabled')
    if s.ai_remote_clean and s.ai_mode=='off' and not s.ocr:raise ValueError('Remote cleaning requires local OCR or AI OCR')
    if (s.ai_mode!='off' or s.ai_remote_clean) and not s.ai_config:
        raise ValueError('AI mode requires an explicitly configured API provider')
    if s.ai_mode=='deepseek-ocr' and s.ai_config:
        from urllib.parse import urlsplit
        host=urlsplit(s.ai_config.base_url).hostname
        if host not in ('api.deepseek.com','127.0.0.1','localhost','::1'):
            raise ValueError('DeepSeek OCR needs the official api.deepseek.com endpoint or explicit localhost test proxy')

def rgb(v,default=None):
    if v is None:return default
    if isinstance(v,(tuple,list)) and len(v)>=3:return tuple(max(0,min(255,int(a))) for a in v[:3])
    v=str(v).strip().lower()
    named={'none':None,'transparent':None,'black':(0,0,0),'white':(255,255,255),'red':(255,0,0),'green':(0,128,0),'blue':(0,0,255),'yellow':(255,255,0),'gray':(128,128,128),'grey':(128,128,128),'navy':(0,0,128),'orange':(255,165,0)}
    if v in named:return named[v]
    if v.startswith('#'):
        h=v[1:]
        if len(h)==3:h=''.join(k*2 for k in h)
        if len(h)==6:
            try:return tuple(bytes.fromhex(h))
            except ValueError:pass
    m=re.fullmatch(r'rgba?\(\s*(\d+)\s*[, ]+\s*(\d+)\s*[, ]+\s*(\d+)(?:\s*[,/ ]+[^)]*)?\)',v)
    return tuple(min(255,int(x)) for x in m.groups()) if m else default

def _num(v,default=0):
    m=re.match(r'\s*('+NUM+')',str(v or ''))
    return float(m.group(1)) if m else default

def _numbers(v):return [float(a) for a in re.findall(NUM,str(v or ''))]

def _clamp_alpha(v,default=1.):
    try:return max(0.,min(1.,float(v)))
    except (ValueError,TypeError):return default


def _svg_style(el,parent):
    s=parent.copy()
    for k in ('fill','stroke','stroke-width','opacity','fill-opacity','stroke-opacity',
              'fill-rule','font-family','font-size','font-weight','text-anchor',
              'display','visibility','stop-color','stop-opacity','clip-path','stroke-dasharray'):
        if el.get(k) is not None:s[k]=el.get(k)
    for part in el.get('style','').split(';'):
        if ':' in part:
            a,b=part.split(':',1);s[a.strip()]=b.strip()
    return s


def _svg_gradients(root):
    """Parse paint-server stops locally; approximate gradients as native PPTX fills."""
    defs={}
    for el in root.iter():
        tag=el.tag.rsplit('}',1)[-1]
        if tag not in ('linearGradient','radialGradient') or not el.get('id'):continue
        stops=[]
        for stop in el:
            if stop.tag.rsplit('}',1)[-1]!='stop':continue
            st=_svg_style(stop,{})
            raw=stop.get('offset','0');pos=(_num(raw)/100) if '%' in raw else _num(raw)
            stops.append({'position':_clamp_alpha(pos,0),'color':rgb(st.get('stop-color','black'),(0,0,0)),
                          'alpha':_clamp_alpha(st.get('stop-opacity',1))})
        stops.sort(key=lambda v:v['position'])
        defs[el.get('id')]={'id':el.get('id'),'type':'linear' if tag=='linearGradient' else 'radial',
                            'stops':stops,'x1':el.get('x1','0%'),'y1':el.get('y1','0%'),
                            'x2':el.get('x2','100%'),'y2':el.get('y2','0%'),
                            'cx':el.get('cx','50%'),'cy':el.get('cy','50%'),
                            'units':el.get('gradientUnits','objectBoundingBox')}
    # Support SVG paint-server inheritance, e.g. gradient href=#base (local references only).
    for el in root.iter():
        tag=el.tag.rsplit('}',1)[-1]
        if tag not in ('linearGradient','radialGradient'):continue
        key=el.get('id');target=(el.get('href') or el.get('{http://www.w3.org/1999/xlink}href') or '')
        if key and key in defs and target.startswith('#') and target[1:] in defs:
            if not defs[key]['stops']:defs[key]['stops']=defs[target[1:]]['stops']
    return defs


def _parse_svg_scene(source):
    root=SafeET.parse(source).getroot()
    if root.tag.rsplit('}',1)[-1]!='svg':raise ValueError('Root element is not SVG')
    vb=_numbers(root.get('viewBox'))
    if len(vb)==4:ox,oy,w,h=vb
    else:ox=oy=0;w=_num(root.get('width'),1000);h=_num(root.get('height'),1000)
    if w<=0 or h<=0:raise ValueError('Invalid SVG viewBox')
    scene=Scene(w,h,[],[])
    from fontmetrics import advance
    initial=(1,0,0,1,-ox,-oy)
    by_id={el.get('id'):el for el in root.iter() if el.get('id')}
    gradients=_svg_gradients(root)
    clip_defs={}
    for node in root.iter():
        if node.tag.rsplit('}',1)[-1]!='clipPath' or not node.get('id'):continue
        if node.get('clipPathUnits','userSpaceOnUse')!='userSpaceOnUse':continue
        children=[c for c in node if c.tag.rsplit('}',1)[-1]=='rect']
        if len(children)==1:
            c=children[0]
            clip_defs[node.get('id')]=(_num(c.get('x')),_num(c.get('y')),
                                      _num(c.get('width')),_num(c.get('height')))

    def add_text(content,x,y,m,style,opacity):
        if not content or not content.strip():return
        px,py=apply_matrix(m,x,y)
        angle=math.degrees(math.atan2(m[1],m[0]));sc=math.hypot(m[0],m[1])
        color=rgb(style.get('fill','black'),(0,0,0)) or (0,0,0)
        scene.add_text(Text(content,px,py,_num(style.get('font-size'),16)*sc,color,
                            style.get('font-family','serif'),angle,style.get('text-anchor','start'),opacity))

    def add_text_element(el,m,style,opacity):
        x=_num(el.get('x'));y=_num(el.get('y'));size=_num(style.get('font-size'),16)
        if el.text:
            add_text(el.text,x,y,m,style,opacity)
            x+=advance(el.text,size,style.get('font-family','serif'))
        for child in el:
            tag=child.tag.rsplit('}',1)[-1]
            if tag!='tspan':continue
            ts=_svg_style(child,{k:v for k,v in style.items() if k!='opacity'})
            tx=_num(child.get('x'),x)+_num(child.get('dx'))
            ty=_num(child.get('y'),y)+_num(child.get('dy'))
            if child.text:add_text(child.text,tx,ty,m,ts,opacity)
            x=tx+advance(child.text or '',_num(ts.get('font-size'),size),ts.get('font-family','serif'));y=ty
            if child.tail:
                add_text(child.tail,x,y,m,style,opacity)
                x+=advance(child.tail,size,style.get('font-family','serif'))

    def walk(el,style,transform,depth=0,alpha=1.,stack=frozenset(),clip=None):
        if depth>80:raise ValueError('SVG nesting too deep')
        tag=el.tag.rsplit('}',1)[-1]
        if tag in ('defs','metadata','title','desc','clipPath','mask','pattern','filter',
                   'linearGradient','radialGradient','symbol','style'):
            return
        # Opacity on groups is composited in SVG; per-item multiplication is an
        # approximation if translucent children overlap. Report it honestly.
        inherited={k:v for k,v in style.items() if k not in ('opacity','clip-path')}
        st=_svg_style(el,inherited)
        opacity=alpha*_clamp_alpha(st.pop('opacity',1))
        if st.get('display')=='none' or st.get('visibility')=='hidden':return
        m=mul(transform,transform_parse(el.get('transform','')))
        if tag=='use':
            href=el.get('href') or el.get('{http://www.w3.org/1999/xlink}href') or ''
            if not href.startswith('#') or href[1:] not in by_id:
                scene.warnings.append('Skipped unresolved or external SVG <use> reference');return
            ref=href[1:]
            if ref in stack:raise ValueError('Recursive SVG <use> reference')
            placement=(1,0,0,1,_num(el.get('x')),_num(el.get('y')))
            walk(by_id[ref],{k:v for k,v in st.items() if k!='clip-path'},mul(m,placement),depth+1,opacity,stack|{ref},clip)
            return
        # Minimal, precise Matplotlib-style rectangular clipPath support.
        own_clip=el.get('clip-path') or st.get('clip-path')
        if own_clip:
            found=re.fullmatch(r'url\(\s*[\"\']?#([^\)\"\']+)[\"\']?\s*\)',own_clip.strip())
            key=found.group(1) if found else None
            if key in clip_defs:
                try:
                    from clipping import rect_clip
                    localclip=rect_clip(*clip_defs[key],m)
                    clip=clip.intersection(localclip) if clip is not None else localclip
                except ImportError:
                    scene.warnings.append('Install shapely for editable rectangular SVG clipping')
            else:
                scene.warnings.append(f'Unsupported clip-path {own_clip}')
        raw_fill=st.get('fill','black')
        gradient=None
        if isinstance(raw_fill,str) and raw_fill.strip().startswith('url('):
            found=re.fullmatch(r'url\(\s*[\"\']?#([^\)\"\']+)[\"\']?\s*\)',raw_fill.strip())
            gradient=gradients.get(found.group(1)) if found else None
            if gradient is None:scene.warnings.append(f'Unresolved fill paint server: {raw_fill}')
        fill=None if raw_fill=='none' else (rgb(raw_fill,(0,0,0)) if gradient is None else None)
        raw_stroke=st.get('stroke')
        stroke_gradient=None
        if isinstance(raw_stroke,str) and raw_stroke.strip().startswith('url('):
            found=re.fullmatch(r'url\(\s*[\"\']?#([^\)\"\']+)[\"\']?\s*\)',raw_stroke.strip())
            stroke_gradient=gradients.get(found.group(1)) if found else None
            if stroke_gradient is None:scene.warnings.append(f'Unresolved stroke paint server: {raw_stroke}')
        stroke=rgb(raw_stroke)
        sw=_num(st.get('stroke-width'),1)
        p=None
        if tag=='path':p=parse_path(el.get('d',''))
        elif tag=='rect':
            x=_num(el.get('x'));y=_num(el.get('y'));ww=_num(el.get('width'));hh=_num(el.get('height'))
            if ww>0 and hh>0:
                rx=_num(el.get('rx'));ry=_num(el.get('ry'),rx)
                if rx>0 or ry>0:
                    rx=min(ww/2,rx or ry);ry=min(hh/2,ry or rx)
                    d=f'M {x+rx} {y} H {x+ww-rx} A {rx} {ry} 0 0 1 {x+ww} {y+ry} V {y+hh-ry} A {rx} {ry} 0 0 1 {x+ww-rx} {y+hh} H {x+rx} A {rx} {ry} 0 0 1 {x} {y+hh-ry} V {y+ry} A {rx} {ry} 0 0 1 {x+rx} {y} Z'
                    p=parse_path(d)
                else:p=polygon([(x,y),(x+ww,y),(x+ww,y+hh),(x,y+hh)])
        elif tag in ('polygon','polyline'):
            vals=_numbers(el.get('points'));p=polygon(list(zip(vals[::2],vals[1::2])),tag=='polygon')
        elif tag=='line':p=polygon([(_num(el.get('x1')),_num(el.get('y1'))),(_num(el.get('x2')),_num(el.get('y2')))],False)
        elif tag in ('circle','ellipse'):
            cx=_num(el.get('cx'));cy=_num(el.get('cy'));rx=_num(el.get('r')) if tag=='circle' else _num(el.get('rx'))
            ry=_num(el.get('r')) if tag=='circle' else _num(el.get('ry'))
            if rx>0 and ry>0:p=ellipse(cx,cy,rx,ry)
        elif tag=='text':
            add_text_element(el,m,st,opacity)
            return
        elif tag in ('image','foreignObject','meshgradient'):
            scene.warnings.append(f'Skipped unsupported SVG element: {tag}')
            return
        if p and p.commands:
            fillrule=st.get('fill-rule','nonzero')
            if fillrule=='evenodd':p=normalize_evenodd(p)
            p=p.transformed(m)
            if clip is not None:
                from clipping import clip_vector
                p,tessellated=clip_vector(p,clip,filled=fill is not None or gradient is not None)
                if tessellated:scene.warnings.append('A clipped SVG curve was locally polygonized to keep its geometry editable')
            if p.commands:
                scene.add_shape(Shape(p,fill,stroke,sw*math.hypot(m[0],m[1]),
                                      opacity*_clamp_alpha(st.get('fill-opacity',1)),
                                      f'{tag}_{len(scene.shapes)+1}',fillrule,
                                      opacity*_clamp_alpha(st.get('stroke-opacity',1)),gradient,
                                      tuple(_numbers(st.get('stroke-dasharray'))) if st.get('stroke-dasharray') not in (None,'none') else None,stroke_gradient))
        if st.get('mask') or st.get('filter'):
            scene.warnings.append(f'Unsupported SVG mask/filter on <{tag}>')
        for child in el:walk(child,st,m,depth+1,opacity,stack,clip)
    walk(root,{},initial)
    return scene

def _load_image(source,max_pixels):
    rgba=ImageOps.exif_transpose(Image.open(source)).convert('RGBA')
    canvas=Image.new('RGBA',rgba.size,'white');canvas.alpha_composite(rgba)
    im=canvas.convert('RGB')
    w,h=im.size
    if w*h>max_pixels:
        scale=math.sqrt(max_pixels/(w*h));im=im.resize((max(1,round(w*scale)),max(1,round(h*scale))),Image.Resampling.LANCZOS)
    return np.array(im)

def _glyph_mask(img,x,y,w,h):
    """Restrict inpainting to likely foreground ink instead of whole OCR rectangles."""
    H,W=img.shape[:2];pad=max(3,round(h*.35))
    x0=max(0,x-pad);y0=max(0,y-pad);x1=min(W,x+w+pad);y1=min(H,y+h+pad)
    roi=img[y0:y1,x0:x1].astype(np.int16)
    if roi.size==0:return np.zeros((0,0),np.uint8),(0,0,0)
    inner=(x-x0,y-y0,x-x0+w,y-y0+h)
    # Pixel samples immediately outside glyph bbox estimate local paper/shape color.
    border=np.ones(roi.shape[:2],bool)
    border[max(0,inner[1]-1):min(roi.shape[0],inner[3]+1),
           max(0,inner[0]-1):min(roi.shape[1],inner[2]+1)]=False
    refs=roi[border] if np.count_nonzero(border)>2 else roi.reshape(-1,3)
    background=np.median(refs,axis=0)
    delta=np.max(np.abs(roi-background[None,None,:]),axis=2)
    threshold=max(20,int(np.percentile(delta[border],90)+10)) if np.any(border) else 28
    binary=np.zeros(roi.shape[:2],np.uint8)
    binary[inner[1]:inner[3],inner[0]:inner[2]]=(delta[inner[1]:inner[3],inner[0]:inner[2]]>=threshold).astype(np.uint8)*255
    # Light dilation erases antialiasing while protecting unrelated background pixels.
    binary=cv2.dilate(binary,np.ones((2,2),np.uint8),iterations=1)
    strong=(binary>0)&(delta>=np.percentile(delta[binary>0],70)) if np.any(binary) else (binary>0)
    ink=roi[strong]
    color=tuple(int(v) for v in np.median(ink,axis=0)) if ink.size else (0,0,0)
    # On flat paper/solid-color blocks, clearing the whole tightly cropped
    # word gives a cleaner result than leaving antialiased glyph fringes.
    # On nonuniform/crossing-line backgrounds, preserve selective ink masking.
    deviation=np.max(np.abs(refs-background[None,:]),axis=1)
    if np.percentile(deviation,95)<10:
        binary[max(0,inner[1]-1):min(binary.shape[0],inner[3]+1),
               max(0,inner[0]-1):min(binary.shape[1],inner[2]+1)]=255
    return binary,color,(x0,y0,x1,y1)


def ocr_image(img,lang='eng',confidence=40,layout='line'):
    """Optional offline Tesseract OCR: group words by line and precisely mask ink."""
    try:import pytesseract
    except ImportError as e:raise RuntimeError('OCR needs pytesseract and the local Tesseract executable') from e
    try:
        info=pytesseract.image_to_data(Image.fromarray(img),lang=lang,output_type=pytesseract.Output.DICT,config='--psm 11')
    except Exception as e:raise RuntimeError(f'Local Tesseract OCR unavailable ({e}). Install the language pack.') from e
    mask=np.zeros(img.shape[:2],dtype=np.uint8);words=[]
    for i,raw in enumerate(info.get('text',[])):
        content=str(raw).strip()
        try:conf=float(info['conf'][i])
        except (ValueError,TypeError,KeyError):continue
        if not content or conf<confidence:continue
        x=max(0,int(info['left'][i]));y=max(0,int(info['top'][i]));w=int(info['width'][i]);h=int(info['height'][i])
        w=min(w,img.shape[1]-x);h=min(h,img.shape[0]-y)
        if w<=0 or h<=0:continue
        ink,ink_color,(x0,y0,x1,y1)=_glyph_mask(img,x,y,w,h)
        if ink.size:np.maximum(mask[y0:y1,x0:x1],ink,out=mask[y0:y1,x0:x1])
        group=tuple(int(info.get(k,[0]*len(info['text']))[i]) for k in ('page_num','block_num','par_num','line_num'))
        words.append({'content':content,'x':x,'y':y,'w':w,'h':h,'color':ink_color,'group':group})
    if layout=='word':
        return [Text(a['content'],a['x'],a['y']+a['h']*.9,max(a['h']*1.28,7),a['color']) for a in words],mask
    lines={}
    for word in words:lines.setdefault(word['group'],[]).append(word)
    texts=[]
    for group,line in lines.items():
        line.sort(key=lambda v:v['x']);first=line[0]
        text=first['content'];lastx=first['x']+first['w']
        for word in line[1:]:
            # Latin words need separation; CJK text normally is tightly connected.
            space=' ' if text and text[-1].isascii() and word['content'][0].isascii() else ''
            text+=space+word['content'];lastx=word['x']+word['w']
        bottom=max(w['y']+w['h'] for w in line)
        h=float(np.median([w['h'] for w in line]));colors=np.median([w['color'] for w in line],axis=0)
        texts.append(Text(text,float(first['x']),bottom+h*.07,max(h*1.30,7),tuple(map(int,colors))))
    return texts,mask


def _smart_inpaint(img,mask,radius=3):
    """Flat backdrop: fill OCR rectangles exactly. Text over complex graphs: Telea."""
    clean=img.copy();leftover=mask.copy()
    n,labels,parts,centroids=cv2.connectedComponentsWithStats(mask,8)
    H,W=mask.shape
    for i in range(1,n):
        x,y,w,h,area=map(int,parts[i])
        if area<40 or area/(w*h)<.75:continue
        pad=max(3,int(h*.25));x0=max(0,x-pad);y0=max(0,y-pad);x1=min(W,x+w+pad);y1=min(H,y+h+pad)
        outside=np.ones((y1-y0,x1-x0),bool)
        outside[y-y0:y-y0+h,x-x0:x-x0+w]=False
        pixels=img[y0:y1,x0:x1][outside].astype(np.int16)
        if len(pixels)<10:continue
        color=np.median(pixels,axis=0)
        deviation=np.max(np.abs(pixels-color),axis=1)
        if np.percentile(deviation,95)<9:
            # Never erase content outside the OCR mask, even with flat paper.
            roi=clean[y:y+h,x:x+w];m=labels[y:y+h,x:x+w]==i
            roi[m]=np.clip(np.rint(color),0,255).astype(np.uint8)
            leftover[y:y+h,x:x+w][m]=0
    if np.any(leftover):clean=cv2.inpaint(clean,leftover,radius,cv2.INPAINT_TELEA)
    return clean


def _trace(img,settings):
    """Pixel palette tracing with proper border background and compound holes."""
    h,w=img.shape[:2]
    palette_image=Image.fromarray(img).quantize(colors=settings.colors,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE)
    idx=np.asarray(palette_image);pal=palette_image.getpalette();areas=Counter(idx.flatten().tolist())
    # Most frequent BORDER paint is a reliable canvas color for flat diagrams;
    # v1 incorrectly used the global average, introducing a tinted background.
    border=np.concatenate((idx[0,:],idx[-1,:],idx[:,0],idx[:,-1]))
    bg_id=int(np.bincount(border.astype(np.int32),minlength=256).argmax())
    base=tuple(map(int,pal[bg_id*3:bg_id*3+3]))
    shapes=[Shape(polygon([(0,0),(w,0),(w,h),(0,h)]),base,name='background')]
    for color_id,area in sorted(areas.items(),key=lambda kv:-kv[1]):
        if color_id==bg_id or area<settings.min_area:continue
        mask=np.where(idx==color_id,255,0).astype(np.uint8)
        contours,hierarchy=cv2.findContours(mask,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:continue
        hierarchy=hierarchy[0];color=tuple(map(int,pal[color_id*3:color_id*3+3]))
        joined=[]
        for ci,(contour,rel) in enumerate(zip(contours,hierarchy)):
            if rel[3]!=-1 or cv2.contourArea(contour)<settings.min_area:continue
            subpaths=[];child=ci
            while child!=-1:
                c=contours[child]
                if cv2.contourArea(c)>=max(2,settings.min_area/3):
                    epsilon=max(.3,settings.simplify*cv2.arcLength(c,True))
                    approx=cv2.approxPolyDP(c,epsilon,True)
                    pts=[tuple(map(float,p[0])) for p in approx]
                    if len(pts)>=3:
                        signed=cv2.contourArea(np.asarray(pts,dtype=np.float32),oriented=True)
                        if (signed>0)!=(child==ci):pts.reverse()
                        subpaths+=polygon(pts).commands
                child=int(hierarchy[child][2]) if child==ci else int(hierarchy[child][0])
            if subpaths:
                if settings.group_regions:joined.extend(subpaths)
                else:shapes.append(Shape(VectorPath(subpaths),color,name=f'region_{color_id}_{ci}'))
                if len(shapes)>=settings.max_shapes:return shapes
        if joined:shapes.append(Shape(VectorPath(joined),color,name=f'color_{color_id}'))
        if len(shapes)>=settings.max_shapes:return shapes
    return shapes


def _trace_vtracer(img,settings):
    """Open-source Rust spline tracing, locally only (opt-in dependency)."""
    try:import vtracer
    except ImportError as e:raise RuntimeError('VTracer is not installed. Install locally: pip install vtracer') from e
    with tempfile.TemporaryDirectory() as temp:
        src=Path(temp)/'input.png';dst=Path(temp)/'paths.svg'
        Image.fromarray(img).save(src)
        if hasattr(vtracer,'convert_file'):
            vtracer.convert_file(str(src),str(dst))
        elif hasattr(vtracer,'convert_image_to_svg_py'):
            vtracer.convert_image_to_svg_py(str(src),str(dst),colormode='color',
                                             hierarchical='stacked',mode='spline',
                                             filter_speckle=max(1,min(128,settings.min_area//4)))
        else:raise RuntimeError('Unsupported VTracer installation: no converter entry point')
        if not dst.exists():raise RuntimeError('VTracer failed to create an SVG')
        scene=_parse_svg_scene(dst)
        if len(scene.shapes)>settings.max_shapes:
            raise ValueError(f'VTracer emitted {len(scene.shapes)} objects (limit {settings.max_shapes}); increase --max-shapes')
        return scene

def _text_from_manifest(path,source_w,source_h):
    if not path:return []
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(data,list):atoms=data;sw=source_w;sh=source_h
    else:
        atoms=data.get('texts',data.get('items',[]));canvas=data.get('source_canvas',{});sw=float(canvas.get('width',source_w));sh=float(canvas.get('height',source_h))
    if sw<=0 or sh<=0:raise ValueError('Invalid manifest source_canvas')
    texts=[]
    for atom in atoms:
        item=atom.get('text',atom) if isinstance(atom.get('text'),dict) else atom
        content=str(item.get('contents',item.get('text','')))
        pos=item.get('position',[item.get('x',0),item.get('y',0)])
        color=rgb(item.get('fillColor',item.get('color','black')),(0,0,0))
        texts.append(Text(content,float(pos[0])*source_w/sw,float(pos[1])*source_h/sh,float(item.get('fontSize',item.get('size',16)))*source_h/sh,color,item.get('fontFamily','Arial'),float(item.get('rotationDegrees',0)),item.get('textAnchor','start'),_clamp_alpha(item.get('opacity',1))))
    return texts

def _manifest_box_mask(img, path):
    """Erase ONLY approved positioned boxes, never invent boxes for free-form text.

    The output of the OCR review workflow is a human-editable JSON manifest.
    Empty/invalid/oversized boxes are SKIPPED rather than erasing whole diagrams.
    """
    doc=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(doc,dict):return np.zeros(img.shape[:2],np.uint8),['Manifest has no positioned boxes']
    canvas=doc.get('source_canvas',{})
    if not isinstance(canvas,dict):raise ValueError('Invalid manifest source_canvas')
    ow=float(canvas.get('width',img.shape[1]));oh=float(canvas.get('height',img.shape[0]))
    if ow<=0 or oh<=0:raise ValueError('Invalid manifest canvas dimensions')
    items=doc.get('texts',[])
    mask=np.zeros(img.shape[:2],np.uint8);warn=[];good=0
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get('bbox'),list) or len(item['bbox'])!=4:continue
        try:
            a,b,c,d=[float(x) for x in item['bbox']]
        except (ValueError,TypeError):continue
        if not (0<=a<c<=ow and 0<=b<d<=oh):
            warn.append('Out-of-canvas reviewed box was ignored');continue
        x=max(0,int(round(a*img.shape[1]/ow)));y=max(0,int(round(b*img.shape[0]/oh)))
        right=min(img.shape[1],int(round(c*img.shape[1]/ow)))
        bottom=min(img.shape[0],int(round(d*img.shape[0]/oh)))
        width=right-x;height=bottom-y
        if width<=0 or height<=0 or width*height>img.shape[0]*img.shape[1]*.09:
            warn.append('Large or empty reviewed box was not erased');continue
        glyph,_,(gx0,gy0,gx1,gy1)=_glyph_mask(img,x,y,width,height)
        if glyph.size:
            np.maximum(mask[gy0:gy1,gx0:gx1],glyph,out=mask[gy0:gy1,gx0:gx1]);good+=1
    if not good:warn.append('No valid box masks in reviewed manifest; no text removed')
    return mask,warn


def _ai_texts_and_mask(img, analysis):
    """Convert model-suggested normalized boxes into editable text + local pixel mask.

    Geometry is estimated rather than an exact OCR guarantee; all coordinates
    are bounded by analysis validation before being applied to the source.
    """
    h,w=img.shape[:2];texts=[];mask=np.zeros((h,w),np.uint8)
    for t in analysis['texts']:
        x0,y0,x1,y1=t['bbox'];x=max(0,int(round(x0*w/1000)));y=max(0,int(round(y0*h/1000)))
        right=min(w,int(round(x1*w/1000)));bottom=min(h,int(round(y1*h/1000)))
        bw=right-x;bh=bottom-y
        if bw<1 or bh<1:continue
        glyph,color,(gx0,gy0,gx1,gy1)=_glyph_mask(img,x,y,bw,bh)
        if glyph.size:np.maximum(mask[gy0:gy1,gx0:gx1],glyph,out=mask[gy0:gy1,gx0:gx1])
        from ai_bridge import AIServiceError
        fallback=rgb(t.get('color'),color) or color
        # Sampled ink color is more grounded when image contrast is unambiguous.
        ink_color=color if np.any(glyph) else fallback
        texts.append(Text(t['text'],float(x),float(bottom),max(5.,float(bh)*1.12),ink_color))
    return texts,mask


def _hybrid_text_reconcile(local_texts, ai_texts, canvas_width, canvas_height):
    """Keep deterministic Tesseract geometry; optionally replace local glyph string
    with a nearby high-confidence AI transcription. AI-only extras are ignored to
    avoid hallucinating chart labels in hybrid mode.
    """
    corrected=[];used=set();changes=0
    for original in local_texts:
        # Local OCR line top estimated from baseline and font-size.
        local_center=(original.x+max(2.,len(original.content)*original.size*.25),
                      original.y-original.size*.42)
        closest=None;best=float('inf');best_index=None
        for i,model in enumerate(ai_texts):
            if i in used:continue
            ai_center=(model.x+max(2.,len(model.content)*model.size*.25),
                       model.y-model.size*.42)
            dx=abs(ai_center[0]-local_center[0]);dy=abs(ai_center[1]-local_center[1])
            if dx>max(45,canvas_width*.08) or dy>max(20,canvas_height*.065):continue
            score=dx/max(1,canvas_width)+2*dy/max(1,canvas_height)
            if score<best:best=score;closest=model;best_index=i
        if closest:
            used.add(best_index)
            # A huge length discrepancy is usually a hallucinated or merged label.
            if 0.4 <= (len(closest.content)+1)/(len(original.content)+1) <= 2.5:
                if closest.content!=original.content:
                    changes+=1
                    original=Text(closest.content,original.x,original.y,original.size,
                                  original.color,original.family,original.angle,original.anchor,original.opacity)
        corrected.append(original)
    return corrected,changes


def _ai_layout_scene(img,analysis):
    """Create fully-editable semantic primitives from the optional vision model."""
    h,w=img.shape[:2]
    borders=np.concatenate((img[0,:,:],img[-1,:,:],img[:,0,:],img[:,-1,:]),axis=0)
    background=tuple(map(int,np.median(borders,axis=0)))
    scene=Scene(float(w),float(h),[],[])
    scene.add_shape(Shape(polygon([(0,0),(w,0),(w,h),(0,h)]),background,name='AI canvas'))
    for i,item in enumerate(analysis['shapes']):
        kind=item['kind'];box=item.get('bbox')
        pts=[(p[0]*w/1000.,p[1]*h/1000.) for p in item.get('points',[])]
        if kind in ('rectangle','ellipse') and box:
            x0,y0,x1,y1=box;x0*=w/1000;y0*=h/1000;x1*=w/1000;y1*=h/1000
            if kind=='rectangle':p=polygon([(x0,y0),(x1,y0),(x1,y1),(x0,y1)])
            else:p=ellipse((x0+x1)/2,(y0+y1)/2,(x1-x0)/2,(y1-y0)/2)
        elif kind in ('line','polyline','arrow') and len(pts)>=2:
            p=polygon(pts,False)
        else:continue
        fill=rgb(item.get('fill'))
        if kind in ('line','polyline','arrow'):fill=None
        stroke=rgb(item.get('stroke'))
        sw=max(0.5,item.get('stroke_width',2.)*max(w,h)/1000)
        scene.add_shape(Shape(p,fill,stroke,sw,name=f'AI {kind} {i+1}'))
        if kind=='arrow' and len(pts)>=2:
            a,b=pts[-2],pts[-1];dx,dy=b[0]-a[0],b[1]-a[1];d=math.hypot(dx,dy)
            if d>1e-4:
                ux,uy=dx/d,dy/d;ln=max(sw*4, min(w,h)*.018)
                tip=b;left=(b[0]-ux*ln-uy*ln*.45,b[1]-uy*ln+ux*ln*.45)
                right=(b[0]-ux*ln+uy*ln*.45,b[1]-uy*ln-ux*ln*.45)
                scene.add_shape(Shape(polygon([tip,left,right]),stroke or (0,0,0),name=f'AI arrowhead {i+1}'))
    txt,_=_ai_texts_and_mask(img,analysis)
    for t in txt:scene.add_text(t)
    scene.warnings.append('AI semantic geometry is approximate; verify line endpoints, text, and dimensions before scientific use')
    return scene


def create_scene(source,settings=None,manifest=None):
    s=settings or Settings();check_settings(s);source=Path(source)
    if not source.is_file():raise FileNotFoundError(source)
    if source.suffix.lower()=='.svg':
        if s.ai_mode!='off' or s.ai_remote_clean:
            raise ValueError('AI features currently apply only to raster images; use native SVG import')
        scene=_parse_svg_scene(source)
        if manifest:
            for t in _text_from_manifest(manifest,scene.width,scene.height):scene.add_text(t)
        return scene,{'source_type':'svg','backend':'native_svg','ocr_detected':0,'ocr_used':False,'warnings':scene.warnings}
    if source.suffix.lower() not in ('.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff'):
        raise ValueError('Use PNG, JPG, WebP, BMP, TIFF or SVG')
    img=_load_image(source,s.max_pixels);h,w=img.shape[:2];original=img.copy();detected=[]
    warns=[];ai_usage={}
    if manifest and s.ai_mode in ('ocr','hybrid','deepseek-ocr'):
        raise ValueError('A curated manifest replaces AI OCR; choose --ai-mode off to avoid an unnecessary paid call')
    if s.ai_mode!='off':
        from ai_bridge import analyze_image
        if s.ai_mode=='deepseek-ocr':
            from deepseek_workflow import recognize
            analysis=recognize(img,s.ai_config,tiling=s.deepseek_tiling,purpose=s.deepseek_purpose)
            warns.extend(analysis.get('warnings',[]))
        else:
            analysis=analyze_image(img,s.ai_config,'ocr' if s.ai_mode=='hybrid' else s.ai_mode)
        ai_usage={'vision_model':analysis['model'],'mode':s.ai_mode}
        if s.ai_mode=='deepseek-ocr':
            ai_usage['page_count']=analysis.get('page_count',0)
            ai_usage['usage']=analysis.get('usage',{})
            ai_usage['request_count']=analysis.get('request_count',1)
            ai_usage['estimated_ocr_cost_cny']=analysis.get('estimated_ocr_cost_cny')
            if not analysis['texts']:
                warns.append('DeepSeek OCR returned no positioned text boxes; no labels were invented')
        if s.ai_mode=='layout':
            scene=_ai_layout_scene(img,analysis)
            if manifest:
                # Keep shapes but prefer corrected text manifest over probabilistic AI text.
                scene.texts.clear();scene.paint_order=[a for a in scene.paint_order if a[0]!='text']
                for t in _text_from_manifest(manifest,scene.width,scene.height):scene.add_text(t)
            if s.clean_preview:warns.append('clean_preview ignored in layout mode')
            return scene,{'source_type':'raster','backend':'ai_layout','ocr_detected':len(analysis['texts']),
                          'ocr_used':bool(scene.texts),'ai':ai_usage,'warnings':scene.warnings+warns}
        if s.ai_mode=='hybrid':
            local_texts,mask=ocr_image(img,s.ocr_lang,s.ocr_confidence,s.ocr_layout)
            model_texts,_=_ai_texts_and_mask(img,analysis)
            detected,changed=_hybrid_text_reconcile(local_texts,model_texts,w,h)
            ai_usage['text_corrections']=changed
            # Preserve local OCR mask so the model cannot arbitrarily enlarge deletion areas.
            warns.append('Hybrid text corrections use local OCR coordinates; review transcriptions')
        else:
            detected,mask=_ai_texts_and_mask(img,analysis)
    elif s.ocr:
        detected,mask=ocr_image(img,s.ocr_lang,s.ocr_confidence,s.ocr_layout)
    else:
        mask=np.zeros(img.shape[:2],dtype=np.uint8)
    if manifest and s.manifest_clean:
        mask,review_warnings=_manifest_box_mask(img,manifest)
        warns.extend(review_warnings)
    if s.inpaint and np.any(mask):
        if s.ai_remote_clean:
            from ai_bridge import edit_remove_text,check_clean_image
            edited,usage=edit_remove_text(img,mask,s.ai_config)
            edited,mae,ok=check_clean_image(img,edited,mask)
            ai_usage.update({'edit_model':s.ai_config.edit_model,'unmasked_mae':mae,
                             'edit_accepted':bool(ok or s.ai_accept_changes)})
            if ok or s.ai_accept_changes:
                img=edited
                if not ok:warns.append(f'User accepted AI edit despite high unmasked MAE ({mae})')
            else:
                warns.append(f'AI edit changed areas outside the text mask (MAE {mae}); used safer local inpainting')
                img=_smart_inpaint(img,mask,s.inpaint_radius)
        else:img=_smart_inpaint(img,mask,s.inpaint_radius)
    elif s.ai_remote_clean:
        raise ValueError('No OCR mask found for AI image edit')
    if s.clean_preview:
        preview=Path(s.clean_preview);preview.parent.mkdir(parents=True,exist_ok=True)
        Image.fromarray(img).save(preview)
    backend=s.backend
    if backend=='auto':backend='precision'
    if backend=='vtracer':
        try:scene=_trace_vtracer(img,s)
        except Exception as exc:
            if s.backend!='auto':raise
            warns.append(f'VTracer failed ({exc}); used local contour fallback')
            backend='contour'
    if backend=='contour':scene=Scene(float(w),float(h),_trace(img,s),[])
    if backend=='precision':
        from precision_trace import trace_scene, TraceOptions
        scene,detail=trace_scene(img,TraceOptions(colors=s.colors,tile_size=s.precision_tile,
                            max_shapes=s.max_shapes,max_commands=s.precision_max_commands,
                            separate_regions=s.precision_separate_regions,
                            min_region_area=s.precision_min_region_area,
                            color_mode=s.color_mode,luminance_weight=s.luminance_weight,
                            gradient_background=s.background_gradient,
                            structure_mode=s.structure_mode))
        warns.append('Precision tracing: '+str(detail))
    if abs(scene.width-w)>1e-4 or abs(scene.height-h)>1e-4:
        scale=(w/scene.width,0,0,h/scene.height,0,0)
        for item in scene.shapes:item.path=item.path.transformed(scale)
        for item in scene.texts:item.x*=scale[0];item.y*=scale[3]
        scene.width=float(w);scene.height=float(h)
    if manifest:
        detected=_text_from_manifest(manifest,w,h)
    for item in detected:scene.add_text(item)
    scene.warnings.extend(warns)
    result={'source_type':'raster','backend':backend,'ocr_detected':len(detected),
            'ocr_used':bool(detected),'warnings':scene.warnings}
    if s.ai_mode!='off' or s.ai_remote_clean:result['ai']=ai_usage
    if s.clean_preview:result['clean_preview']=str(s.clean_preview)
    return scene,result

def _colorhex(c):return ''.join(f'{v:02X}' for v in c)

def _xml_escape(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;').replace("'",'&apos;')

def _gradient_xml(gradient,alpha):
    """Approximate simple SVG paint servers using native, editable DrawingML fills."""
    if not gradient or not gradient.get('stops'):return '<a:noFill/>'
    parts=[]
    for stop in gradient['stops']:
        pos=max(0,min(100000,round(stop['position']*100000)))
        c=_colorhex(stop['color']);opacity=max(0,min(100000,round(alpha*stop.get('alpha',1)*100000)))
        color=f'<a:srgbClr val="{c}">'+(f'<a:alpha val="{opacity}"/>' if opacity<100000 else '')+'</a:srgbClr>'
        parts.append(f'<a:gs pos="{pos}">{color}</a:gs>')
    if gradient['type']=='radial':
        geometry='<a:path path="circle"><a:fillToRect l="0" t="0" r="0" b="0"/></a:path>'
    else:
        def fraction(v,default):
            return _num(v,default)/100 if '%' in str(v) else _num(v,default)
        x1=fraction(gradient.get('x1','0%'),0);y1=fraction(gradient.get('y1','0%'),0)
        x2=fraction(gradient.get('x2','100%'),100);y2=fraction(gradient.get('y2','0%'),0)
        angle=(math.degrees(math.atan2(y2-y1,x2-x1))%360)*60000
        geometry=f'<a:lin ang="{round(angle)}" scaled="1"/>'
    return '<a:gradFill rotWithShape="1"><a:gsLst>'+''.join(parts)+'</a:gsLst>'+geometry+'</a:gradFill>'


def _add_ooxml_path(slide,shape,scene,slidew,slideh,index):
    if not shape.path.commands:return
    x0,y0,x1,y1=path_bbox(shape.path)
    if not all(map(math.isfinite,(x0,y0,x1,y1))):raise ValueError('Nonfinite SVG geometry')
    # EMU geometry entirely local to each editable PowerPoint object.
    extent=100000;bw=max(x1-x0,1e-6);bh=max(y1-y0,1e-6)
    def local(x,y):return (round((x-x0)/bw*extent),round((y-y0)/bh*extent))
    segments=[]
    for op,args in shape.path.commands:
        if op in ('M','L'):
            x,y=local(*args);segments.append(f'<a:{"moveTo" if op=="M" else "lnTo"}><a:pt x="{x}" y="{y}"/></a:{"moveTo" if op=="M" else "lnTo"}>')
        elif op=='C':
            pts=[local(*args[j:j+2]) for j in (0,2,4)]
            segments.append('<a:cubicBezTo>'+''.join(f'<a:pt x="{x}" y="{y}"/>' for x,y in pts)+'</a:cubicBezTo>')
        elif op=='Z':segments.append('<a:close/>')
    if not segments:return
    fill=_gradient_xml(shape.gradient,shape.opacity) if shape.gradient else ('<a:noFill/>' if shape.fill is None else f'<a:solidFill><a:srgbClr val="{_colorhex(shape.fill)}"/></a:solidFill>')
    stroke='<a:ln><a:noFill/></a:ln>'
    if shape.stroke is not None or shape.stroke_gradient is not None:
        width=max(1270,int(shape.stroke_width*slidew/scene.width))
        alpha_stroke=round(shape.stroke_opacity*100000)
        sc=(f'<a:srgbClr val="{_colorhex(shape.stroke)}">'+(f'<a:alpha val="{alpha_stroke}"/>' if alpha_stroke<100000 else '')+'</a:srgbClr>') if shape.stroke is not None else ''
        custom=''
        if shape.dasharray:
            nums=[max(1,round(n*100000/max(shape.stroke_width,1e-6))) for n in shape.dasharray]
            if len(nums)%2:nums=nums*2
            pairs=zip(nums[::2],nums[1::2])
            custom='<a:custDash>'+''.join(f'<a:ds d="{d}" sp="{sp}"/>' for d,sp in pairs)+'</a:custDash>'
        linefill=_gradient_xml(shape.stroke_gradient,shape.stroke_opacity) if shape.stroke_gradient else f'<a:solidFill>{sc}</a:solidFill>'
        stroke=f'<a:ln w="{width}">{linefill}{custom}</a:ln>'
    sx=slidew/scene.width;sy=slideh/scene.height
    left=round(x0*sx);top=round(y0*sy);ww=max(1,round(bw*sx));hh=max(1,round(bh*sy))
    alpha=round(shape.opacity*100000)
    # PowerPoint supports alpha applied to each shape via solid fill.
    if alpha<100000 and shape.fill is not None:
        fill=f'<a:solidFill><a:srgbClr val="{_colorhex(shape.fill)}"><a:alpha val="{alpha}"/></a:srgbClr></a:solidFill>'
    # Preserve editable Office presets for recognized simple geometry.
    geometry = (f'<a:prstGeom prst="{shape.primitive}"><a:avLst/></a:prstGeom>'
                if shape.primitive in ('rect','ellipse') else
                f'<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="l" t="t" r="r" b="b"/>'
                f'<a:pathLst><a:path w="{extent}" h="{extent}">{"".join(segments)}</a:path></a:pathLst></a:custGeom>')
    xml=f'''<p:sp {nsdecls('a','p')}>
    <p:nvSpPr><p:cNvPr id="{slide.shapes._next_shape_id}" name="{_xml_escape(shape.name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
    <p:spPr><a:xfrm><a:off x="{left}" y="{top}"/><a:ext cx="{ww}" cy="{hh}"/></a:xfrm>
    {geometry}{fill}{stroke}</p:spPr>
    <p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>'''
    slide.shapes._spTree.insert_element_before(parse_xml(xml),'p:extLst')

def _add_text(slide,t,scene,sw,sh):
    if not t.content.strip():return
    sx=sw/scene.width;sy=sh/scene.height
    fontpt=max(3.,t.size*sy/12700.)
    w=max(t.size*2,len(t.content)*t.size*.85)*sx
    x=t.x*sx
    if t.anchor in ('middle','center'):x-=w/2
    elif t.anchor=='end':x-=w
    top=(t.y-t.size*1.05)*sy;boxh=t.size*1.8*sy
    if t.angle:
        anchorx=t.x*sx;anchory=t.y*sy
        dx=x+w/2-anchorx;dy=top+boxh/2-anchory
        a=math.radians(t.angle)
        rx=dx*math.cos(a)-dy*math.sin(a);ry=dx*math.sin(a)+dy*math.cos(a)
        x=anchorx+rx-w/2;top=anchory+ry-boxh/2
    box=slide.shapes.add_textbox(round(x),round(top),round(w),round(boxh))
    box.name='Editable Text: '+t.content[:36];box.rotation=t.angle
    tf=box.text_frame;tf.clear();tf.word_wrap=False
    tf.margin_top=tf.margin_bottom=tf.margin_left=tf.margin_right=0
    p=tf.paragraphs[0];p.alignment={'middle':PP_ALIGN.CENTER,'center':PP_ALIGN.CENTER,'end':PP_ALIGN.RIGHT}.get(t.anchor,PP_ALIGN.LEFT)
    run=p.add_run();run.text=t.content;run.font.name=t.family;run.font.size=Pt(fontpt);run.font.color.rgb=RGBColor(*t.color)
    if t.opacity<1:
        solid=run._r.rPr.solidFill
        rgb_color=solid.srgbClr
        alpha=OxmlElement('a:alpha');alpha.set('val',str(max(0,min(100000,round(t.opacity*100000)))))
        rgb_color.append(alpha)

def write_pptx(scene,output,slide_width=12.):
    prs=Presentation();prs.slide_width=Inches(slide_width);prs.slide_height=Inches(slide_width*scene.height/scene.width)
    slide=prs.slides.add_slide(prs.slide_layouts[6]);sw=prs.slide_width;sh=prs.slide_height
    for kind,i in scene.order():
        if kind=='shape':_add_ooxml_path(slide,scene.shapes[i],scene,sw,sh,i)
        else:_add_text(slide,scene.texts[i],scene,sw,sh)
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True);prs.save(str(output))
    return output

def _path_d(path):
    return ' '.join(op+' '+','.join(f'{a:.9g}' for a in args) if args else op for op,args in path.commands)

def write_svg(scene,output):
    root=ET.Element('svg',xmlns='http://www.w3.org/2000/svg',viewBox=f'0 0 {scene.width:g} {scene.height:g}',
                    width=f'{scene.width:g}',height=f'{scene.height:g}')
    referenced={sh.gradient['id']:sh.gradient for sh in scene.shapes if sh.gradient}
    referenced.update({sh.stroke_gradient['id']:sh.stroke_gradient for sh in scene.shapes if sh.stroke_gradient})
    if referenced:
        defs=ET.SubElement(root,'defs')
        for key,g in referenced.items():
            tag='linearGradient' if g['type']=='linear' else 'radialGradient'
            attrs={'id':key,'gradientUnits':g.get('units','objectBoundingBox')}
            for k in ('x1','y1','x2','y2') if g['type']=='linear' else ('cx','cy'):
                attrs[k]=g[k]
            el=ET.SubElement(defs,tag,attrs)
            for stop in g['stops']:
                ET.SubElement(el,'stop',{'offset':str(stop['position']),
                                         'stop-color':'#'+_colorhex(stop['color']),
                                         'stop-opacity':str(stop['alpha'])})
    graph=getattr(scene,'structure_graph',None)
    metadata={o['shape_index']:o for o in graph.get('objects',[])} if graph else {}
    repetition={ind:grp['group_id'] for grp in graph.get('repetition_groups',[])
                for ind in grp['member_shape_indices']} if graph else {}
    for kind,i in scene.order():
        if kind=='shape':
            sh=scene.shapes[i]
            fill=('url(#'+sh.gradient['id']+')') if sh.gradient else ('none' if sh.fill is None else '#'+_colorhex(sh.fill))
            attrs={'fill':fill,
                'stroke':('url(#'+sh.stroke_gradient['id']+')') if sh.stroke_gradient else ('none' if sh.stroke is None else '#'+_colorhex(sh.stroke)),
                'stroke-width':str(sh.stroke_width),'fill-opacity':str(sh.opacity),
                'stroke-opacity':str(sh.stroke_opacity),'fill-rule':'nonzero'}
            if sh.primitive == 'ellipse':
                x0,y0,x1,y1=path_bbox(sh.path)
                attrs.update({'cx':str((x0+x1)/2),'cy':str((y0+y1)/2),
                              'rx':str((x1-x0)/2),'ry':str((y1-y0)/2)})
                e=ET.SubElement(root,'ellipse',attrs)
            elif sh.primitive == 'rect':
                x0,y0,x1,y1=path_bbox(sh.path)
                attrs.update({'x':str(x0),'y':str(y0),'width':str(x1-x0),'height':str(y1-y0)})
                e=ET.SubElement(root,'rect',attrs)
            elif sh.primitive == 'line':
                coords=[a for op,a in sh.path.commands if op in ('M','L')]
                if len(coords)>=2:
                    attrs.update({'x1':str(coords[0][0]),'y1':str(coords[0][1]),
                                  'x2':str(coords[1][0]),'y2':str(coords[1][1])})
                    e=ET.SubElement(root,'line',attrs)
                else:
                    attrs['d']=_path_d(sh.path);e=ET.SubElement(root,'path',attrs)
            else:
                attrs['d']=_path_d(sh.path);e=ET.SubElement(root,'path',attrs)
            if sh.dasharray:e.set('stroke-dasharray',' '.join(str(v) for v in sh.dasharray))
            e.set('data-name',sh.name)
            if i in metadata:e.set('data-structure-kind',metadata[i]['kind'])
            if i in repetition:e.set('data-repetition-group',repetition[i])
        else:
            t=scene.texts[i]
            e=ET.SubElement(root,'text',{'x':str(t.x),'y':str(t.y),'font-size':str(t.size),
                'font-family':t.family,'text-anchor':t.anchor,'fill':'#'+_colorhex(t.color),'opacity':str(t.opacity)})
            if t.angle:e.set('transform',f'rotate({t.angle:g} {t.x:g} {t.y:g})')
            e.text=t.content
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    ET.ElementTree(root).write(output,encoding='utf-8',xml_declaration=True)
    return output

def write_pdf(svg_path,pdf_path):
    try:import cairosvg
    except ImportError as e:raise RuntimeError('PDF export requires optional cairosvg: pip install cairosvg') from e
    Path(pdf_path).parent.mkdir(parents=True,exist_ok=True)
    cairosvg.svg2pdf(url=str(svg_path),write_to=str(pdf_path));return Path(pdf_path)

def manifest(scene,ocr_count=None):
    return {'source_canvas':{'width':scene.width,'height':scene.height},'texts':[{'contents':t.content,'position':[t.x,t.y],'fontSize':t.size,'fillColor':list(t.color),'fontFamily':t.family,'rotationDegrees':t.angle,'textAnchor':t.anchor,'opacity':t.opacity} for t in scene.texts]}

def _conversion_preflight(source,output,settings,manifest_path,extra_svg,extra_pdf,manifest_output,report_path):
    """Fail before a potentially chargeable AI request; never overwrite source files."""
    check_settings(settings)
    src=Path(source).resolve()
    if not src.is_file():raise FileNotFoundError(src)
    output=Path(output)
    if output.resolve()==src:
        raise ValueError('Never overwrite the input image')
    if output.suffix.lower() not in ('.pptx','.svg','.pdf'):
        raise ValueError('Output must be .pptx, .svg or .pdf')
    paths={'source':src, 'output':output.resolve()}
    if manifest_path:paths['manifest']=Path(manifest_path).resolve()
    for name,val in [('svg_out',extra_svg),('pdf_out',extra_pdf),('manifest_out',manifest_output),
                     ('report',report_path),('clean_preview',settings.clean_preview)]:
        if val:paths[name]=Path(val).resolve()
    seen={}
    for label,path in paths.items():
        if path in seen:
            raise ValueError(f'{label} must not overwrite {seen[path]} ({path.name})')
        seen[path]=label
    if settings.ai_mode!='off' and manifest_path and settings.ai_mode in ('ocr','hybrid','deepseek-ocr'):
        raise ValueError('A curated manifest replaces AI OCR; choose --ai-mode off to avoid an unnecessary paid call')
    if manifest_path:
        try:json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        except Exception as e:raise ValueError('Manifest must be an existing valid JSON file') from e
    if (output.suffix.lower()=='.pdf' or extra_pdf or report_path) and not importlib.util.find_spec('cairosvg'):
        raise RuntimeError('Install optional cairosvg for vector PDF or quality report before starting a chargeable task')
    if settings.backend=='vtracer' and not importlib.util.find_spec('vtracer'):
        raise RuntimeError('Install optional vtracer before starting a chargeable task')
    if (settings.ocr or settings.ai_mode=='hybrid') and not importlib.util.find_spec('pytesseract'):
        raise RuntimeError('Install pytesseract and Tesseract to enable local or hybrid OCR')


def convert(source,output,settings=None,manifest_path=None,extra_svg=None,extra_pdf=None,manifest_output=None,report_path=None,*,edit_plan=None,scene_graph_out=None):
    s=settings or Settings()
    _conversion_preflight(source,output,s,manifest_path,extra_svg,extra_pdf,manifest_output,report_path)
    if scene_graph_out:
        existing = [Path(source).resolve(),Path(output).resolve()]
        existing.extend(Path(p).resolve() for p in (manifest_path,extra_svg,extra_pdf,manifest_output,report_path) if p)
        if Path(scene_graph_out).resolve() in existing:
            raise ValueError('scene graph output must not overwrite source or any conversion artifact')
    scene,stats=create_scene(source,s,manifest_path)
    if edit_plan:
        from visual_editor import apply_edit_plan, source_signature
        plan=json.loads(Path(edit_plan).read_text(encoding='utf-8')) if isinstance(edit_plan,(str,Path)) else edit_plan
        if plan.get('ocr_text_ids') and not manifest_path:
            raise ValueError('画布曾移动 OCR 文字框：导出时必须同时使用最新的已审核文字 JSON（--manifest）。')
        apply_edit_plan(scene,plan,source_signature(Path(source),s))
        stats['visual_edits']=(len(plan.get('shape_moves',[]))+len(plan.get('native_text_moves',[]))+
                               len(plan.get('ocr_text_ids',[])))
    output=Path(output);fmt=output.suffix.lower()
    if fmt=='.pptx':write_pptx(scene,output,s.slide_width)
    elif fmt=='.svg':write_svg(scene,output)
    elif fmt=='.pdf':
        with tempfile.TemporaryDirectory() as tmp:
            temp=Path(tmp)/'intermediate.svg';write_svg(scene,temp);write_pdf(temp,output)
    else:raise ValueError('Output must be .pptx, .svg or .pdf')
    if extra_svg:write_svg(scene,extra_svg)
    if extra_pdf:
        with tempfile.TemporaryDirectory() as tmp:
            temp=Path(tmp)/'intermediate.svg';write_svg(scene,temp);write_pdf(temp,extra_pdf)
    if manifest_output:
        p=Path(manifest_output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(manifest(scene),ensure_ascii=False,indent=2),encoding='utf-8')
    if scene_graph_out:
        target=Path(scene_graph_out);target.parent.mkdir(parents=True,exist_ok=True)
        graph=getattr(scene,'structure_graph', {'schema_version':1,'objects':[], 'repetition_groups':[],
                                               'disclaimer':'No native raster structure proposal available.'})
        target.write_text(json.dumps(graph,ensure_ascii=False,indent=2),encoding='utf-8')
    result={'output':str(output),'shapes':len(scene.shapes),'texts':len(scene.texts),
            'canvas':[scene.width,scene.height],**stats}
    if report_path:
        from quality_report import build_quality_report
        if extra_svg:svg_report=Path(extra_svg)
        elif fmt=='.svg':svg_report=output
        else:
            # Ensure the temp file exists until SVG raster comparison completes.
            with tempfile.TemporaryDirectory() as tmp:
                svg_report=Path(tmp)/'editable.svg';write_svg(scene,svg_report)
                result['quality']=build_quality_report(source,svg_report,report_path,result)
                result['report']=str(report_path)
                return result
        result['quality']=build_quality_report(source,svg_report,report_path,result)
        result['report']=str(report_path)
    return result
