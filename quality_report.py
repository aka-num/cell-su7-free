"""Offline visual fidelity check. Outputs a self-contained HTML A/B comparator.

The scores compare a rendered editable SVG against the source pixels and do
NOT establish parity with other products, or correctness of object semantics.
"""
from __future__ import annotations
import base64, html, io, json, math, re
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageOps


def _png(im):
    out=io.BytesIO();im.save(out,format='PNG');return out.getvalue()


def _source_preview(path,size):
    path=Path(path)
    if path.suffix.lower()=='.svg':
        try:import cairosvg
        except ImportError as e:raise RuntimeError('HTML quality report needs cairosvg for SVG inputs') from e
        raster=cairosvg.svg2png(url=str(path),output_width=size[0],output_height=size[1])
        return Image.open(io.BytesIO(raster)).convert('RGB')
    im=ImageOps.exif_transpose(Image.open(path)).convert('RGBA')
    bg=Image.new('RGBA',im.size,'white');bg.alpha_composite(im)
    return bg.convert('RGB').resize(size,Image.Resampling.LANCZOS)


def build_quality_report(source,editable_svg,report_path,stats=None,preview_width=1000):
    try:import cairosvg
    except ImportError as e:raise RuntimeError('HTML quality report needs cairosvg: pip install cairosvg') from e
    from defusedxml import ElementTree as ET
    svg_root=ET.parse(editable_svg).getroot();vb=svg_root.get('viewBox','').split()
    if len(vb)!=4:raise ValueError('Editable SVG needs a viewBox for fidelity report')
    w,h=round(float(vb[2])),round(float(vb[3]))
    if w<=0 or h<=0:raise ValueError('Invalid vector canvas dimensions')
    scale=min(1.,preview_width/max(w,h),math.sqrt(1_200_000/(w*h)))
    size=(max(1,round(w*scale)),max(1,round(h*scale)))
    original=_source_preview(source,size)
    processed=cairosvg.svg2png(url=str(editable_svg),output_width=size[0],output_height=size[1])
    vector=Image.open(io.BytesIO(processed)).convert('RGB')
    a=np.asarray(original).astype(np.float32);b=np.asarray(vector).astype(np.float32)
    diff=np.abs(a-b)
    mae=float(diff.mean());mse=float(((a-b)**2).mean())
    psnr=float(10*math.log10(255**2/mse)) if mse>0 else None
    pixel_match=float(np.mean(np.max(diff,axis=2)<=12))
    # Local structural similarity on luminance; report as a diagnostic, not a guarantee.
    ay=cv2.cvtColor(a.astype(np.uint8),cv2.COLOR_RGB2GRAY).astype(np.float32)
    by=cv2.cvtColor(b.astype(np.uint8),cv2.COLOR_RGB2GRAY).astype(np.float32)
    mux=cv2.GaussianBlur(ay,(11,11),1.5);muy=cv2.GaussianBlur(by,(11,11),1.5)
    vx=cv2.GaussianBlur(ay*ay,(11,11),1.5)-mux*mux
    vy=cv2.GaussianBlur(by*by,(11,11),1.5)-muy*muy
    cov=cv2.GaussianBlur(ay*by,(11,11),1.5)-mux*muy
    c1=(.01*255)**2;c2=(.03*255)**2
    ssim=float(np.mean((2*mux*muy+c1)*(2*cov+c2)/((mux*mux+muy*muy+c1)*(vx+vy+c2))))
    def cie_lab(rgb):
        lab=cv2.cvtColor(rgb.astype(np.uint8),cv2.COLOR_RGB2LAB).astype(np.float32)
        lab[...,0]*=100./255.;lab[...,1:]-=128.
        return lab
    de=np.linalg.norm(cie_lab(a)-cie_lab(b),axis=2)
    metrics={'mae':round(mae,3),'psnr_db':round(psnr,2) if psnr is not None else None,
             'ssim':round(ssim,4),'match_within_12':round(pixel_match*100,2),
             'mean_delta_e76':round(float(np.mean(de)),3),
             'p95_delta_e76':round(float(np.percentile(de,95)),3),
             'compared_at':list(size)}
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True)
    u1='data:image/png;base64,'+base64.b64encode(_png(original)).decode()
    u2='data:image/png;base64,'+base64.b64encode(_png(vector)).decode()
    # SVG-vs-source ΔE76 heatmap (perceptual *diagnostic*, not calibrated ΔE2000).
    colors=cv2.applyColorMap(np.uint8(np.clip(de*4,0,255)),cv2.COLORMAP_TURBO)
    heatmap=Image.fromarray(cv2.cvtColor(colors,cv2.COLOR_BGR2RGB))
    heatmap_uri='data:image/png;base64,'+base64.b64encode(_png(heatmap)).decode()
    metrics_js=json.dumps(metrics,ensure_ascii=False)
    stats_json=json.dumps(stats or {},ensure_ascii=False,indent=2)
    source_name=html.escape(Path(source).name);output_name=html.escape(Path(editable_svg).name)
    doc='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cell SU7 | 本地重建质量报告</title><style>
:root{font-family:Inter,system-ui,"Microsoft Yahei",sans-serif;color:#e8edff;background:#0d1324}
*{box-sizing:border-box}body{margin:0;padding:32px 22px}main{max-width:1120px;margin:auto}
h1{font-size:30px;margin:8px 0 4px;letter-spacing:-.03em}p{color:#a4b0ca;line-height:1.65}
.eyebrow{color:#81a9ff;font-size:12px;font-weight:800;letter-spacing:.18em}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:24px 0}
.stat{background:#18243b;border:1px solid #2d3b55;border-radius:15px;padding:19px}
.number{font-size:27px;font-weight:800;white-space:nowrap}.label{font-size:12px;color:#a7b2c9;margin-top:7px}
.card{background:#131e31;border:1px solid #314158;padding:20px;border-radius:18px}
.controls{display:flex;align-items:center;gap:16px;margin:4px 0 18px}.controls input{flex:1;accent-color:#79a3f8}
.canvas{position:relative;width:100%;overflow:hidden;border-radius:10px;line-height:0;background:#fff}
.canvas img{display:block;width:100%;height:auto}.above{position:absolute;inset:0;clip-path:inset(0 50% 0 0)}
.divider{position:absolute;z-index:2;top:0;bottom:0;left:50%;width:2px;background:#81aaff;pointer-events:none}
.corner{position:absolute;top:15px;z-index:4;background:#101a2ddd;color:#fff;padding:9px 12px;border-radius:8px;font-size:12px;line-height:1}
.left{left:15px}.right{right:15px}.foot{font-size:12px;margin-top:14px}
pre{white-space:pre-wrap;word-break:break-word;color:#aebbd2;background:#0c1628;padding:15px;border-radius:10px}
@media(max-width:650px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:24px}}
</style></head><body><main><div class="eyebrow">CELL SU7 / REMASTERED / QUALITY LAB</div>
<h1>可编辑图形重建 · 质量报告</h1><p>所有比较在本机完成，图片嵌入当前 HTML。拖动滑块观察图层、线条、颜色及文字偏差。像素分数只能衡量视觉接近程度，不能衡量图形语义或可编辑性。</p>
<section class="stats"><div class="stat"><div class="number" id="mae"></div><div class="label">平均绝对像素误差 · 越低越好</div></div>
<div class="stat"><div class="number" id="psnr"></div><div class="label">PSNR（dB）· 越高越好</div></div>
<div class="stat"><div class="number" id="ssim"></div><div class="label">SSIM · 越接近 1 越好</div></div>
<div class="stat"><div class="number" id="match"></div><div class="label">误差不超过 12 的像素比例</div></div>
<div class="stat"><div class="number" id="de"></div><div class="label">平均 ΔE76（Lab，越低越好）</div></div>
<div class="stat"><div class="number" id="de95"></div><div class="label">P95 ΔE76（越低越好）</div></div></section>
<section class="card"><div class="controls"><b>原图</b><input id="slider" type="range" value="50" min="0" max="100" aria-label="原图与重建图比较"><b>重建</b></div>
<div class="canvas"><img src="__AFTER__" alt="本地重建预览"><div class="above" id="above"><img src="__BEFORE__" alt="原图"></div>
<div class="divider" id="divider"></div><span class="corner left">原图</span><span class="corner right">重建图</span></div>
<p class="foot">原始文件：__SOURCE__ · 预览文件：__OUTPUT__ · 对比画布：__SIZE__</p></section>
<h2>色差热力图（蓝色小，红色大）</h2><section class="card"><img style="width:100%;display:block" src="__HEATMAP__" alt="每个像素的 ΔE76 色差热力图"></section><h2>执行信息</h2><pre>__STATS__</pre><p>注意：PPTX/Illustrator 等应用的原生渲染结果可能与 SVG 预览不同；复杂渐变、滤镜、外部 SVG 内容和文字字体替换仍须人工检查。</p>
</main><script>const m=__METRICS__;document.querySelector('#mae').textContent=m.mae;
document.querySelector('#psnr').textContent=m.psnr_db===null?'∞':m.psnr_db;
document.querySelector('#ssim').textContent=m.ssim;
document.querySelector('#match').textContent=m.match_within_12+'%';
document.querySelector('#de').textContent=m.mean_delta_e76;
document.querySelector('#de95').textContent=m.p95_delta_e76;
const slider=document.querySelector('#slider'),above=document.querySelector('#above'),divider=document.querySelector('#divider');
slider.addEventListener('input',()=>{above.style.clipPath=`inset(0 ${100-slider.value}% 0 0)`;divider.style.left=slider.value+'%'});
</script></body></html>'''
    doc=(doc.replace('__AFTER__',u2).replace('__BEFORE__',u1)
            .replace('__SOURCE__',source_name).replace('__OUTPUT__',output_name)
            .replace('__SIZE__',f'{size[0]} × {size[1]}').replace('__HEATMAP__',heatmap_uri).replace('__STATS__',html.escape(stats_json))
            .replace('__METRICS__',metrics_js))
    report_path.write_text(doc,encoding='utf-8')
    return metrics
