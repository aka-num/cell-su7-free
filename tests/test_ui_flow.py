"""GUI-independent behavior: use under pytest without an X server."""
from __future__ import annotations
import json
from pathlib import Path
from PIL import Image
import pytest
from ui_flow import StudioState, local_ocr_manifest


def _make_source(tmp_path,size=(200,100)):
    p=tmp_path/'source.png';Image.new('RGB',size,'white').save(p)
    return p


def _manifest():
    return {'source_canvas':{'width':200,'height':100},'review_required':True,'texts':[
        {'id':1,'contents':'TLR','bbox':[3,5,41,26],'position':[3,26],'fontSize':20,'reviewed':False}
    ]}


def test_import_resets_previous_document(tmp_path):
    state=StudioState();p=_make_source(tmp_path);state.open_source(p)
    state.set_manifest(_manifest());state.approve()
    p2=tmp_path/'second.png';Image.new('RGB',(80,90)).save(p2)
    state.open_source(p2)
    assert state.manifest is None and not state.approved and state.output.name=='second_editable.pptx'


def test_reviewed_manifest_can_be_edited_saved_reloaded(tmp_path):
    state=StudioState();state.open_source(_make_source(tmp_path))
    state.set_manifest(_manifest())
    state.change_text(1,'NLRP3',[4,6,52,27],18)
    with pytest.raises(ValueError,match='去字前'):
        state.offline_export_options(clean=True)
    state.approve()
    path=state.save_manifest(tmp_path/'review.json')
    payload=json.loads(path.read_text(encoding='utf-8'))
    assert payload['texts'][0]['contents']=='NLRP3'
    assert payload['texts'][0]['position']==[4,27]
    again=StudioState();again.open_source(state.source);again.load_manifest(path)
    assert again.approved and again.offline_export_options(clean=True).manifest_clean


def test_bad_boxes_are_rejected_even_if_document_looks_valid(tmp_path):
    state=StudioState();state.open_source(_make_source(tmp_path))
    doc=_manifest();doc['texts'][0]['bbox']=[0,0,500,44]
    with pytest.raises(ValueError,match='画布内'):state.set_manifest(doc)
    doc=_manifest();doc['source_canvas']['width']=1000
    with pytest.raises(ValueError,match='不匹配'):state.set_manifest(doc)


def test_changing_or_removing_resets_review_approval(tmp_path):
    state=StudioState();state.open_source(_make_source(tmp_path));state.set_manifest(_manifest());state.approve()
    state.change_text(1,'ASC',[1,2,29,20])
    assert not state.approved
    state.approve();state.remove_text(1)
    assert not state.approved and not state.has_texts


def test_new_text_has_editable_geometry_and_no_cloud_modes(tmp_path):
    state=StudioState();state.open_source(_make_source(tmp_path))
    ident=state.change_text(None,'AIM2',[10,12,60,38],13)
    assert ident==1 and state.review_count==1
    settings=state.offline_export_options(clean=False,backend='contour')
    assert settings.ai_mode=='off' and not settings.ocr and not settings.ai_remote_clean
    assert settings.backend=='contour'


def test_local_ocr_groups_text_with_exact_word_bbox():
    img=Image.new('RGB',(200,100),'white')
    d={'text':['Staphylococcus','aureus','noise'],'conf':['94','90','10'],
       'left':[10,116,50],'top':[20,20,40],'width':[101,49,20],'height':[15,16,9],
       'page_num':[1]*3,'block_num':[1]*3,'par_num':[1]*3,'line_num':[1,1,2]}
    manifest=local_ocr_manifest(img,d)
    assert len(manifest['texts'])==1
    assert manifest['texts'][0]['contents']=='Staphylococcus aureus'
    assert manifest['texts'][0]['bbox']==[10,20,165,36]


def test_invalid_input_never_overwrites_existing_source(tmp_path):
    src=_make_source(tmp_path);before=src.read_bytes();state=StudioState();state.open_source(src)
    with pytest.raises(ValueError,match='覆盖原图'):state.save_manifest(src) if state.manifest else (
        state.set_manifest(_manifest()),state.save_manifest(src))
    assert src.read_bytes()==before


def test_review_manifest_cannot_be_used_with_wrong_image(tmp_path):
    state=StudioState();state.open_source(_make_source(tmp_path));state.set_manifest(_manifest())
    path=state.save_manifest(tmp_path/'ocr.json')
    another=tmp_path/'another.png';Image.new('RGB',(320,120)).save(another)
    state.open_source(another)
    with pytest.raises(ValueError,match='不匹配'):state.load_manifest(path)
