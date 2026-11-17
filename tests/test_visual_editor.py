import copy
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from PIL import Image
from pptx import Presentation

from ai_bridge import parse_deepseek_ocr, AIConfig
from deepseek_workflow import recognize
from engine import Scene, Shape, Text, Settings, create_scene, convert
from geometry import polygon, path_bbox
from visual_editor import EditableDocument, apply_edit_plan, source_signature


def _scene():
    return Scene(300.,200.,[Shape(polygon([(10,15),(45,15),(45,35),(10,35)]),fill=(255,0,0))],
                 [Text('AIM2',80,30,12)])


def test_shape_drag_is_native_geometry_and_undo_redo(tmp_path):
    source=tmp_path/'source.png';Image.new('RGB',(300,200),'white').save(source)
    identity=source_signature(source,Settings())
    ed=EditableDocument(_scene(),identity)
    assert ed.select(20,22,kind_filter='shape')==('shape',0)
    assert ed.select(90,22,kind_filter='text')==('text',0)
    ed.move('shape',0,26,14)
    assert path_bbox(ed.scene.shapes[0].path)==pytest.approx((36,29,71,49))
    ed.undo();assert path_bbox(ed.scene.shapes[0].path)==pytest.approx((10,15,45,35))
    ed.redo();assert path_bbox(ed.scene.shapes[0].path)==pytest.approx((36,29,71,49))
    plan=ed.plan();fresh=_scene()
    apply_edit_plan(fresh,plan,identity)
    assert path_bbox(fresh.shapes[0].path)==pytest.approx((36,29,71,49))
    assert len(plan['shape_moves'])==1


def test_manifest_text_drag_persists_and_no_double_shift(tmp_path):
    source=tmp_path/'source.png';Image.new('RGB',(300,200),'white').save(source)
    m={'source_canvas':{'width':300,'height':200},'texts':[
        {'id':11,'contents':'NLRP3','bbox':[80,30,135,45], 'position':[80,45],
         'fontSize':14,'reviewed':True}]}
    scene=_scene()
    ed=EditableDocument(scene,source_signature(source,Settings()),m)
    ed.move('text',0,10,8)
    assert ed.manifest['texts'][0]['bbox']==pytest.approx([90,38,145,53])
    assert ed.manifest['texts'][0]['position']==pytest.approx([90,53])
    assert ed.manifest['texts'][0]['reviewed'] is False
    assert 11 in ed.changed_text_ids
    assert ed.plan()['native_text_moves']==[] # OCR changes go through manifest, not offsets
    assert ed.plan()['ocr_text_ids']==[11]
    ed.undo()
    assert ed.manifest['texts'][0]['bbox']==pytest.approx([80,30,135,45])
    ed.redo()
    assert ed.manifest['texts'][0]['bbox']==pytest.approx([90,38,145,53])


def test_plan_rejects_new_source_or_changed_geometry(tmp_path):
    s=tmp_path/'sample.png';Image.new('RGB',(300,200),'white').save(s)
    ident=source_signature(s,Settings())
    ed=EditableDocument(_scene(),ident);ed.move('shape',0,12,0);p=ed.plan()
    other=copy.deepcopy(ident);other['settings']['colors']=32
    with pytest.raises(ValueError,match='不匹配'):apply_edit_plan(_scene(),p,other)
    changed=_scene();changed.shapes[0].path=changed.shapes[0].path.transformed((1,0,0,1,50,0))
    with pytest.raises(ValueError,match='位置已变化'):apply_edit_plan(changed,p,ident)


def test_original_svg_convert_exports_real_moved_objects(tmp_path):
    svg=tmp_path/'source.svg'
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
                   '<rect x="25" y="30" width="70" height="40" fill="#00aa44"/>'
                   '<text x="110" y="60" font-size="14">TEST</text></svg>')
    setting=Settings()
    original,_=create_scene(svg,setting)
    first=path_bbox(original.shapes[0].path)
    doc=EditableDocument(original,source_signature(svg,setting))
    doc.move('shape',0,36,22)
    doc.move('text',0,8,4)
    plan=doc.plan()
    target=tmp_path/'moved.pptx'
    outsvg=tmp_path/'moved.svg'
    result=convert(svg,target,setting,extra_svg=outsvg,edit_plan=plan)
    assert target.is_file() and outsvg.is_file()
    assert result['visual_edits']==2
    assert 'TEST' in outsvg.read_text()
    assert 'x="118' in outsvg.read_text() or 'x="118.0' in outsvg.read_text()
    from pptx import Presentation
    prs=Presentation(target)
    shapes=prs.slides[0].shapes
    assert any(z.has_text_frame and 'TEST' in z.text for z in shapes)
    rebuilt,_=create_scene(svg,setting)
    apply_edit_plan(rebuilt,plan,source_signature(svg,setting))
    now=path_bbox(rebuilt.shapes[0].path)
    assert now==pytest.approx((first[0]+36,first[1]+22,first[2]+36,first[3]+22))


def test_length_ocr_partial_keeps_only_complete_pairs_and_no_retry():
    calls=[]
    def fake(img,cfg,*,purpose):
        calls.append(1)
        return parse_deepseek_ocr({'pages':[{'index':0,'text':
            '<｜ref｜>NLRP3<｜/ref｜><｜box｜>[[200,350,300,380]]<｜/box｜>'
            '<｜ref｜>unfinished','finish_reason':'length'}]})
    img=np.ones((120,260,3),dtype=np.uint8)*255
    data=recognize(img,AIConfig(api_key='not-a-real-key'),recognizer=fake)
    assert calls==[1]
    assert data['partial'] and data['partial_tiles']==[1]
    assert data['texts'][0]['text']=='NLRP3'
    assert any('不完整' in s for s in data['warnings'])
