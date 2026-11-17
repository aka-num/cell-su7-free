"""v4.2 structure-aware primitive reconstruction and fidelity guards."""
from pathlib import Path
import json,zipfile,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import cv2
from PIL import Image
from structure_aware import fit_primitive, repeated_groups
from engine import Settings, create_scene, convert, write_svg


def make_synthetic():
    img=np.full((160,240,3),245,dtype=np.uint8)
    # Separate small repeated circles (same color, cross different 64px tiles)
    for cx in (28,81,136,189):cv2.circle(img,(cx,36),10,(52,182,81),-1)
    cv2.rectangle(img,(22,74),(62,102),(220,74,57),-1)
    cv2.line(img,(92,94),(170,94),(51,51,51),1)
    # Complex blue protein-like silhouette must stay an editable freeform.
    poly=np.array([(70,128),(85,110),(108,120),(121,109),(134,123),(113,146),(88,149)],np.int32)
    cv2.fillPoly(img,[poly],(53,90,180))
    return img


def test_simple_primitive_fit_and_no_false_confidence():
    p=fit_primitive(np.ones((3,40),bool),10,20)
    assert p.kind=='native_line' and not p.fill and p.stroke_width==3
    p=fit_primitive(np.ones((20,30),bool),3,6)
    assert p.kind=='native_rectangle' and p.iou==1
    mask=np.zeros((64,64),np.uint8);cv2.circle(mask,(32,32),15,1,-1)
    fitted=fit_primitive(mask.astype(bool),0,0)
    # Conservative fitter may decline a discretized circle, never falsely fit.
    assert fitted is None or fitted.iou>=.985
    irregular=np.zeros((40,40),np.uint8)
    irregular[2:15,3:17]=1;irregular[18:38,20:38]=1
    assert fit_primitive(irregular.astype(bool),0,0) is None


def test_grouping_requires_same_color_shape_and_size():
    a=[]
    for i in range(4):
        a.append({'shape_index':i,'kind':'native_ellipse','bbox':[i*30,0,i*30+16,16],
                  'rgb':[20,190,40]})
    a.append({'shape_index':5,'kind':'native_ellipse','bbox':[0,30,17,47],'rgb':[215,35,80]})
    groups=repeated_groups(a)
    assert len(groups)==1
    assert groups[0]['member_shape_indices']==[0,1,2,3]
    assert len(groups[0]['instances'])==4


def test_objects_mode_editable_svg_and_pptx(tmp_path):
    path=tmp_path/'src.png';Image.fromarray(make_synthetic()).save(path)
    settings=Settings(colors=12,background_gradient=False,color_mode='legacy',structure_mode='objects',
                      precision_tile=64,max_shapes=4000)
    scene,_=create_scene(path,settings)
    g=scene.structure_graph
    assert g['schema_version']==2
    assert g['native_primitive_count']>0
    assert len(g['objects'])==len(scene.shapes)
    assert any(o['kind']=='native_rectangle' for o in g['objects'])
    assert all(0<=o['shape_index']<len(scene.shapes) for o in g['objects'])
    svg=tmp_path/'out.svg';write_svg(scene,svg)
    txt=svg.read_text()
    assert '<rect' in txt or '<line' in txt
    ppt=tmp_path/'out.pptx'
    graph=tmp_path/'graph.json'
    convert(path,ppt,settings,extra_svg=svg,scene_graph_out=graph)
    with zipfile.ZipFile(ppt) as z:
        xml=z.read('ppt/slides/slide1.xml')
        assert b'<a:prstGeom prst="rect"' in xml or b'<a:prstGeom prst="ellipse"' in xml
        assert b'<a:custGeom>' in xml
        assert not any(s.startswith('ppt/media/') for s in z.namelist())
    assert json.loads(graph.read_text())['native_primitive_count']>0


def test_off_mode_no_native_promotions(tmp_path):
    path=tmp_path/'a.png';Image.fromarray(make_synthetic()).save(path)
    a,_=create_scene(path,Settings(colors=8,backend='precision',color_mode='legacy',
                                   background_gradient=False,structure_mode='off',precision_tile=64))
    assert not any(getattr(sh,'primitive',None) for sh in a.shapes)


def test_visual_editor_group_drag_export_and_undo(tmp_path):
    from engine import Scene, Shape, polygon, write_pptx
    from visual_editor import EditableDocument, apply_edit_plan
    shapes=[]
    scene=Scene(120,80,[],[])
    for i in range(3):
        scene.add_shape(Shape(polygon([(i*24+2,10),(i*24+15,10),(i*24+15,22),(i*24+2,22)]),
                              (0,150,60),name=f'unit{i}'))
    scene.structure_graph={'repetition_groups':[{'group_id':'units',
                          'member_shape_indices':[0,1,2]}]}
    signature={'source':'unit-test'}
    editor=EditableDocument(scene,signature)
    assert editor.select(3,11,kind_filter='group')==('group','units')
    origins=[editor.box('shape',i) for i in range(3)]
    editor.move('group','units',7,4)
    assert editor.box('shape',1)[0]==origins[1][0]+7
    assert editor.undo()
    assert editor.box('shape',1)==origins[1]
    assert editor.redo()
    ppt=tmp_path/'group.pptx';write_pptx(scene,ppt)
    assert ppt.is_file()
    plan=editor.plan();assert len(plan['shape_moves'])==3
    from copy import deepcopy
    fresh=Scene(120,80,[],[])
    for i in range(3):
        fresh.add_shape(Shape(polygon([(i*24+2,10),(i*24+15,10),(i*24+15,22),(i*24+2,22)]),
                              (0,150,60),name=f'unit{i}'))
    apply_edit_plan(fresh,plan,signature)
    assert all(fresh.shapes[i].path.commands==scene.shapes[i].path.commands for i in range(3))


def test_synthetic_filled_arrow_candidate_preserves_nearly_all_pixels():
    mask=np.zeros((50,80),np.uint8)
    poly=np.array([(10,15),(40,15),(40,5),(60,20),(40,35),(40,25),(10,25)],np.int32)
    cv2.fillPoly(mask,[poly],1)
    result=fit_primitive(mask.astype(bool),0,0)
    assert result is not None and result.kind=='native_arrow_candidate'
    assert result.iou>.98 and result.primitive=='polygon'
