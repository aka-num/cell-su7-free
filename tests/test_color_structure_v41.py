"""Regression checks for offline colors, structure proposals, native exports."""
from pathlib import Path
import json,sys,zipfile
import numpy as np
from PIL import Image,ImageDraw
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from perceptual_color import estimate_background,build_color_plan
from engine import Settings,create_scene,convert
from visual_editor import source_signature


def synthetic_gradient():
    h,w=110,180
    rows=np.linspace(0,1,h)[:,None]
    top=np.array([240,250,240]);bottom=np.array([222,236,252])
    im=np.uint8(np.clip(np.rint((1-rows)*top+rows*bottom),0,255))
    im=np.broadcast_to(im[:,None,:],(h,w,3)).copy()
    im[24:48,30:75]=[49,175,72]
    im[50:84,92:130]=[212,65,90]
    return im


def test_gradient_uses_native_stops_without_covering_saturated_shapes():
    im=synthetic_gradient()
    _,mask,g=estimate_background(im)
    assert g and g['type']=='linear'
    assert g['stops'][0]['color'][1] >= 240
    assert g['stops'][-1]['color'][2] >= 242
    assert mask[10,10] and not mask[30,50] and not mask[65,100]


def test_flat_color_background_does_not_invent_gradient():
    im=np.full((90,100,3),230,dtype=np.uint8)
    _,mask,g=estimate_background(im)
    assert g is None and not mask.any()


def test_perceptual_recovers_saturated_green_not_grayscale():
    im=synthetic_gradient()
    plan=build_color_plan(im,12,'perceptual',True)
    ident=plan.ids[30,50]
    assert ident >= 0
    red,green,blue=plan.palette[ident]
    assert green >= red+40 and green >= blue+30
    assert plan.diagnostics['background_coverage'] > .3


def test_raster_scene_native_objects_and_scene_graph(tmp_path):
    im=Image.fromarray(synthetic_gradient())
    p=tmp_path/'input.png';im.save(p)
    scene,stats=create_scene(p,Settings(colors=12,backend='precision',color_mode='perceptual',
                                         background_gradient=True,structure_mode='hybrid',
                                         precision_tile=96,precision_min_region_area=24))
    graph=scene.structure_graph
    assert len(graph['objects'])==len(scene.shapes)
    assert graph['objects'][0]['kind']=='gradient_background'
    assert all(0<=o['shape_index']<len(scene.shapes) for o in graph['objects'])
    assert scene.shapes[0].gradient and scene.shapes[0].gradient['stops']
    out=tmp_path/'out.pptx';sg=tmp_path/'scene.json'
    convert(p,out,Settings(colors=12,backend='precision',color_mode='perceptual',
              background_gradient=True,structure_mode='hybrid',precision_tile=96,
              precision_min_region_area=24),scene_graph_out=sg)
    assert json.loads(sg.read_text())['objects']
    with zipfile.ZipFile(out) as archive:
        xml=archive.read('ppt/slides/slide1.xml')
        assert b'gradFill' in xml
        assert b'custGeom' in xml
        assert not any(s.startswith('ppt/media/') for s in archive.namelist())


def test_color_change_invalidates_old_visual_edits(tmp_path):
    img=tmp_path/'test.png';Image.new('RGB',(80,80),'white').save(img)
    a=source_signature(img,Settings(color_mode='balanced'))
    b=source_signature(img,Settings(color_mode='perceptual'))
    assert a!=b


def test_scene_graph_cannot_overwrite_original(tmp_path):
    src=tmp_path/'original.png';Image.new('RGB',(33,33),'white').save(src)
    before=src.read_bytes()
    with pytest.raises(ValueError,match='must not overwrite'):
        convert(src,tmp_path/'out.svg',Settings(backend='precision',colors=2),scene_graph_out=src)
    assert src.read_bytes()==before


def test_quality_report_contains_delta_e_metrics(tmp_path):
    pytest.importorskip('cairosvg')
    src=tmp_path/'image.png';Image.fromarray(synthetic_gradient()).save(src)
    out=tmp_path/'image.svg';report=tmp_path/'report.html'
    result=convert(src,out,Settings(backend='precision',color_mode='perceptual',colors=12),report_path=report)
    assert 'mean_delta_e76' in result['quality']
    assert '色差热力图' in report.read_text(encoding='utf-8')
