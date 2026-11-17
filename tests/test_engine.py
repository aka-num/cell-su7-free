from pathlib import Path
import sys, json, zipfile, xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image,ImageDraw
from pptx import Presentation
from engine import Settings, convert, create_scene, write_svg, manifest
from geometry import parse_path,arc_to_cubics

def test_full_svg_curves(tmp_path):
    svg=tmp_path/'input.svg';ppt=tmp_path/'out.pptx'
    svg.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200"><rect width="300" height="200" fill="#ffffff"/><g transform="translate(10,10)"><path d="M 20 20 C 25 0 100 0 120 20 Q 150 50 120 90 A 20 20 0 0 1 80 90 Z" fill="#4455aa"/><text x="30" y="120" font-size="22">Label 12</text></g></svg>''',encoding='utf-8')
    result=convert(svg,ppt)
    assert result['shapes']==2 and result['texts']==1
    pres=Presentation(ppt);slide=pres.slides[0]
    assert len(slide.shapes)==3
    assert any(sh.text=='Label 12' for sh in slide.shapes if sh.has_text_frame)
    with zipfile.ZipFile(ppt) as z:
        xml=z.read('ppt/slides/slide1.xml').decode()
        assert '<a:cubicBezTo>' in xml

def test_raster_editable_and_svg(tmp_path):
    im=Image.new('RGB',(180,120),'white');d=ImageDraw.Draw(im)
    d.rectangle((20,20,120,95),fill='navy');d.ellipse((60,37,94,75),fill='white')
    src=tmp_path/'a.png';im.save(src)
    ppt=tmp_path/'a.pptx';svg=tmp_path/'a.svg'
    result=convert(src,ppt,Settings(colors=4,min_area=10),extra_svg=svg)
    assert result['shapes']>=2
    assert len(Presentation(ppt).slides[0].shapes)==result['shapes']
    root=ET.parse(svg).getroot();assert root.tag.endswith('svg')
    assert len(root.findall('{http://www.w3.org/2000/svg}path'))==result['shapes']

def test_manifest_roundtrip(tmp_path):
    img=Image.new('RGB',(100,100),'white');src=tmp_path/'image.png';img.save(src)
    mf=tmp_path/'text.json';mf.write_text(json.dumps({'source_canvas':{'width':100,'height':100},'texts':[{'contents':'温度 T','position':[12,30],'fontSize':15,'fillColor':[10,20,30]}]},ensure_ascii=False),encoding='utf-8')
    out=tmp_path/'out.pptx';generated=tmp_path/'ocr.json'
    res=convert(src,out,Settings(colors=2),manifest_path=mf,manifest_output=generated)
    assert res['texts']==1
    assert '温度 T' in [x.text for x in Presentation(out).slides[0].shapes if x.has_text_frame]
    assert json.loads(generated.read_text(encoding='utf-8'))['texts'][0]['contents']=='温度 T'

def test_svg_opacity_and_path_parser():
    p=parse_path('M10 10 h40 v20 l-5 7 s10 20 30 40 t30 20 a10 20 0 1 1 10 -15 z')
    assert len(p.commands)>5 and p.commands[-1][0]=='Z'
    assert any(op=='C' for op,_ in p.commands)

def test_svg_transform(tmp_path):
    source=tmp_path/'in.svg'
    source.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><g transform="translate(25,30)"><circle cx="20" cy="20" r="10" fill="blue"/></g></svg>')
    scene,_=create_scene(source)
    assert 30 < min(x for op,args in scene.shapes[0].path.commands for x in args[::2]) < 40

def test_ocr_optin(tmp_path):
    from PIL import ImageFont
    import pytesseract
    src=tmp_path/'text.png'
    im=Image.new('RGB',(420,130),'white');d=ImageDraw.Draw(im)
    d.text((30,24),'HELLO WORLD',font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',35),fill='black')
    im.save(src)
    scene,stats=create_scene(src,Settings(ocr=True,ocr_lang='eng'))
    assert stats['ocr_used'] and stats['ocr_detected']>=1
    assert len(scene.texts)>=1
