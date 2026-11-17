"""Regression tests for painter order, gradients, holes, local vector fidelity."""
from pathlib import Path
import io, json, sys, zipfile, xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pytest
from PIL import Image,ImageDraw
from pptx import Presentation
from engine import Settings,convert,create_scene,write_svg,write_pptx,ocr_image
from geometry import parse_path,normalize_evenodd,_flatten_subpath,_area


def test_border_color_is_real_background(tmp_path):
    im=Image.new('RGB',(220,160),'#fffdf6');d=ImageDraw.Draw(im)
    d.rectangle((12,12,200,125),fill='#182c70')
    d.ellipse((50,30,130,110),fill='#ff9900')
    src=tmp_path/'img.png';im.save(src)
    scene,stats=create_scene(src,Settings(backend='contour',colors=3,min_area=5))
    assert scene.shapes[0].name=='background'
    assert np.max(np.abs(np.array(scene.shapes[0].fill)-np.array([255,253,246])))<3
    assert stats['backend']=='contour'


def test_svg_painter_order_and_unique_pptx_ids(tmp_path):
    src=tmp_path/'in.svg';out=tmp_path/'out.pptx';extra=tmp_path/'out.svg'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 120">
    <rect width="200" height="120" fill="white"/>
    <text x="15" y="34" font-size="15">BEHIND</text>
    <rect x="0" y="45" width="200" height="25" fill="blue"/>
    <text x="15" y="90" font-size="15">FRONT</text></svg>''')
    scene,_=create_scene(src)
    assert scene.order()==[('shape',0),('text',0),('shape',1),('text',1)]
    convert(src,out,extra_svg=extra)
    slide=Presentation(out).slides[0]
    assert [a.text if a.has_text_frame and a.text else a.name for a in slide.shapes]==[
        'rect_1','BEHIND','rect_2','FRONT'] or [
        'rect_1','BEHIND','rect_2','FRONT'] == [
        a.name if not a.has_text_frame or not a.text else a.text for a in slide.shapes]
    with zipfile.ZipFile(out) as z:
        root=ET.fromstring(z.read('ppt/slides/slide1.xml'))
        ids=[el.attrib['id'] for el in root.iter() if el.tag.endswith('}cNvPr')]
        assert len(ids)==len(set(ids)),ids
    root=ET.parse(extra).getroot();tags=[el.tag.rsplit('}',1)[-1] for el in root if el.tag.endswith(('path','text'))]
    assert tags==['path','text','path','text']


def test_svg_evenodd_contours_get_opposite_winding(tmp_path):
    p=parse_path('M 0 0 L 90 0 L 90 90 L 0 90 Z M 20 20 L 70 20 L 70 70 L 20 70 Z')
    norm=normalize_evenodd(p)
    chunks=[];cur=[]
    for op,args in norm.commands:
        if op=='M' and cur:chunks.append(cur);cur=[]
        cur.append((op,args))
    if cur:chunks.append(cur)
    areas=[_area(_flatten_subpath(x)) for x in chunks]
    assert areas[0]*areas[1]<0,areas
    src=tmp_path/'holes.svg'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 90 90">
    <path fill="#333" fill-rule="evenodd" d="M0 0 H90 V90 H0 Z M20 20 H70 V70 H20 Z"/></svg>''')
    scene,_=create_scene(src)
    assert scene.shapes[0].fill_rule=='evenodd'
    assert len(scene.shapes[0].path.commands)>=10


def test_use_def_reference_and_tspans(tmp_path):
    src=tmp_path/'use.svg'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 150">
    <defs><circle id="dot" cx="10" cy="10" r="5"/></defs>
    <use href="#dot" x="30" y="40" fill="red"/>
    <text x="20" y="100">Hello<tspan x="20" y="122" fill="blue">WORLD</tspan></text></svg>''')
    scene,_=create_scene(src)
    assert len(scene.shapes)==1
    assert scene.shapes[0].fill==(255,0,0)
    assert len(scene.texts)==2
    assert [t.content for t in scene.texts]==['Hello','WORLD']
    assert scene.texts[1].y==122


def test_linear_gradient_export_native_pptx(tmp_path):
    src=tmp_path/'gradient.svg';ppt=tmp_path/'gradient.pptx';svg=tmp_path/'gradient_out.svg'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 130">
    <defs><linearGradient id="accent" x1="0%" x2="100%"><stop offset="0%" stop-color="#ff9900"/>
    <stop offset="100%" stop-color="#2030cc" stop-opacity="0.75"/></linearGradient></defs>
    <rect x="20" y="20" width="190" height="90" fill="url(#accent)"/></svg>''')
    result=convert(src,ppt,extra_svg=svg)
    assert result['shapes']==1
    with zipfile.ZipFile(ppt) as z:
        data=z.read('ppt/slides/slide1.xml').decode()
        assert '<a:gradFill' in data and '<a:lin' in data and '<a:alpha val="75000"' in data
    root=ET.parse(svg).getroot();assert any(x.tag.endswith('linearGradient') for x in root.iter())


def test_vtracer_missing_does_not_fake_success(tmp_path,monkeypatch):
    src=tmp_path/'a.png';Image.new('RGB',(40,40),'white').save(src)
    import engine
    def fail(*args,**kwargs):raise RuntimeError('intentionally unavailable')
    monkeypatch.setattr(engine,'_trace_vtracer',fail)
    scene,info=create_scene(src,Settings(backend='vtracer')) if False else (None,None)
    with pytest.raises(RuntimeError,match='intentionally unavailable'):
        create_scene(src,Settings(backend='vtracer'))


def test_ocr_line_group_and_selective_mask():
    from PIL import ImageFont
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',30)
    im=Image.new('RGB',(370,100),'white');d=ImageDraw.Draw(im)
    d.text((24,20),'VECTOR TEST',font=font,fill='black')
    try:texts,mask=ocr_image(np.array(im),layout='line')
    except RuntimeError as e:pytest.skip(str(e))
    assert any('VECTOR' in t.content for t in texts)
    assert mask.max()==255
    # Flat backgrounds may use tight rectangles, while complex backgrounds
    # retain glyph-selective masks. Neither method erases the full canvas.
    assert 0 < np.count_nonzero(mask) < mask.size*.25


def test_matplotlib_style_rect_clip_path(tmp_path):
    src=tmp_path/'clip.svg'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
    <defs><clipPath id="clip"><rect x="30" y="20" width="80" height="60"/></clipPath></defs>
    <g clip-path="url(#clip)"><path d="M 0 40 L 150 40" fill="none" stroke="blue" stroke-width="3"/>
    <rect x="10" y="35" width="130" height="20" fill="red" opacity="0.2"/></g></svg>''')
    scene,_=create_scene(src)
    assert len(scene.shapes)==2
    for shape in scene.shapes:
        xs=[x for op,a in shape.path.commands for x in a[::2]]
        assert xs and min(xs)>=29.999 and max(xs)<=110.001,(shape.name,xs)


def test_dashed_stroke_and_translucent_text(tmp_path):
    src=tmp_path/'dash.svg';ppt=tmp_path/'dash.pptx'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
    <path d="M10 30 H180" stroke="blue" stroke-width="2" stroke-dasharray="6,4" fill="none"/>
    <text x="15" y="80" opacity="0.5">Transparent</text></svg>''')
    scene,_=create_scene(src)
    assert scene.shapes[0].dasharray==(6,4)
    assert scene.texts[0].opacity==0.5
    convert(src,ppt)
    with zipfile.ZipFile(ppt) as z:
        xml=z.read('ppt/slides/slide1.xml').decode()
        assert '<a:custDash>' in xml and '<a:alpha val="50000"' in xml


def test_manifest_overrides_ocr_not_duplicate(tmp_path):
    from PIL import ImageFont
    im=Image.new('RGB',(260,90),'white');draw=ImageDraw.Draw(im)
    draw.text((20,20),'HELLO',font=ImageFont.truetype('DejaVuSans.ttf',30),fill='black')
    img=tmp_path/'sample.png';im.save(img)
    mf=tmp_path/'manifest.json'
    mf.write_text(json.dumps({'texts':[{'text':'Reviewed text','x':20,'y':50,'size':32}]}))
    try:scene,stats=create_scene(img,Settings(ocr=True),manifest=mf)
    except RuntimeError as e:pytest.skip(str(e))
    assert stats['ocr_detected']>=1
    assert len(scene.texts)==1 and scene.texts[0].content=='Reviewed text'


def test_quality_report_roundtrip(tmp_path):
    from quality_report import build_quality_report
    pytest.importorskip('cairosvg')
    img=Image.new('RGB',(100,60),'white');draw=ImageDraw.Draw(img)
    draw.rectangle((10,8,50,42),fill='navy')
    src=tmp_path/'a.png';img.save(src)
    out=tmp_path/'out.pptx';svg=tmp_path/'out.svg';report=tmp_path/'quality.html'
    result=convert(src,out,Settings(colors=2,backend='contour',min_area=3),extra_svg=svg,report_path=report)
    assert report.exists() and '质量报告' in report.read_text()
    assert 0<=result['quality']['ssim']<=1.001
    assert result['quality']['compared_at']==[100,60]
    assert len(Presentation(out).slides[0].shapes)>0


def test_gradient_href_local_inheritance(tmp_path):
    src=tmp_path/'grad.svg'
    src.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"><defs>
    <linearGradient id="base"><stop offset="0%" stop-color="red"/><stop offset="100%" stop-color="blue"/></linearGradient>
    <linearGradient id="child" href="#base" x1="0%" x2="100%"/></defs>
    <path d="M5 5 H95 V70 H5 Z" fill="url(#child)"/></svg>''')
    scene,_=create_scene(src)
    assert len(scene.shapes)==1
    assert scene.shapes[0].gradient['id']=='child'
    assert len(scene.shapes[0].gradient['stops'])==2


def test_optional_vtracer_local_adapter_with_fake_backend(tmp_path,monkeypatch):
    """Adapter contract test; no network, no native Rust package installed here."""
    import types
    source=tmp_path/'in.png';Image.new('RGB',(80,60),'white').save(source)
    calls=[]
    def convert_file(inp,out):
        calls.append((inp,out))
        Path(out).write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 60">
        <path fill="blue" d="M10 10 H70 V50 H10 Z"/></svg>''')
    monkeypatch.setitem(sys.modules,'vtracer',types.SimpleNamespace(convert_file=convert_file))
    scene,info=create_scene(source,Settings(backend='vtracer'))
    assert calls and info['backend']=='vtracer' and len(scene.shapes)==1
    assert scene.shapes[0].fill==(0,0,255)


def test_pptx_contains_only_native_shapes_and_text(tmp_path):
    img=tmp_path/'image.svg';out=tmp_path/'native.pptx'
    img.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
    <rect width="100" height="100" fill="white"/>
    <circle cx="50" cy="50" r="20" fill="red"/>
    <text x="30" y="90">Label</text></svg>''')
    convert(img,out)
    with zipfile.ZipFile(out) as archive:
        media=[n for n in archive.namelist() if n.startswith('ppt/media/')]
        assert media==[],f'Unexpected raster slide image assets: {media}'
    slide=Presentation(out).slides[0]
    assert len(slide.shapes)==3
    assert sum(bool(x.has_text_frame and x.text) for x in slide.shapes)==1
