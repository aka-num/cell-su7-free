"""DeepSeek November 2026 doc-contract tests. All remote calls are mocked/local."""
from pathlib import Path
import json
import sys
import numpy as np
from PIL import Image
from pptx import Presentation
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from ai_bridge import AIConfig, AIServiceError, parse_deepseek_ocr, deepseek_ocr_image
from deepseek_workflow import (tile_regions,recognize,manifest_from_ocr,
                               write_review_bundle,flash_review,_deduplicate)
from engine import Settings, convert


def _response(text, reason='stop'):
    return {'object':'ocr.result','page_count':1,
            'pages':[{'index':0,'text':text,'finish_reason':reason}],
            'usage':{'input_tokens':20,'output_tokens':15,'total_tokens':35}}


def test_official_fullwidth_boxes_and_unpositioned_are_not_fabricated():
    raw='<｜ref｜>Listeria monocytogenes<｜/ref｜><｜box｜>[[50,15,280,38]]<｜/box｜>\n' \
        '<｜ref｜>unpositioned label<｜/ref｜> extra Markdown'
    v=parse_deepseek_ocr(_response(raw))
    assert len(v['texts'])==1
    assert v['texts'][0]['text']=='Listeria monocytogenes'
    assert v['unpositioned']==1
    assert v['texts'][0].get('confidence') is None  # API does not provide a confidence score


def test_ascii_box_variant_and_invalid_boxes():
    text='<|ref|>NLRP3<|/ref|><|box|>[[230,350,320,390]]<|/box|>'
    assert parse_deepseek_ocr(_response(text))['texts'][0]['bbox']==[230,350,320,390]
    bad='<｜ref｜>nonsense<｜/ref｜><｜box｜>[[5,7,1,10]]<｜/box｜>'
    assert parse_deepseek_ocr(_response(bad))['texts']==[]


def test_multi_box_merging_is_flagged():
    text='<｜ref｜>two lines<｜/ref｜><｜box｜>[[50,10,100,20],[40,30,105,40]]<｜/box｜>'
    result=parse_deepseek_ocr(_response(text))
    assert result['texts'][0]['bbox']==[40,10,105,40]
    assert 'multi-box' in ' '.join(result['warnings'])


def test_truncation_preserves_complete_boxes_but_requires_review_and_errors_fail_closed():
    t='<｜ref｜>short<｜/ref｜><｜box｜>[[5,10,40,35]]<｜/box｜>'
    partial=parse_deepseek_ocr(_response(t+'<｜ref｜>unfinished','length'))
    assert partial['partial'] is True and partial['needs_review']
    assert [v['text'] for v in partial['texts']]==['short']
    assert any('不完整' in w or '长度上限' in w for w in partial['warnings'])
    with pytest.raises(AIServiceError,match='error'):
        parse_deepseek_ocr({'error':{'message':'Sensitive provider details should never escape'}})


def test_multipage_is_rejected_for_single_image_editor():
    obj=_response('text')
    obj['pages'].append(dict(obj['pages'][0],index=1))
    with pytest.raises(AIServiceError,match='multi-page'):
        parse_deepseek_ocr(obj)


def test_tiling_regions_and_cost_preflight():
    assert tile_regions(1000,600,'single')==[(0,0,1000,600)]
    reg=tile_regions(1000,600,'2x2')
    assert len(reg)==4
    assert all(x0<=500<=x1 and y0<=300<=y1 for x0,y0,x1,y1 in reg)
    assert len(tile_regions(1000,600,'2x2'))==4
    with pytest.raises(ValueError):tile_regions(1000,600,'8x8')


def test_four_tiles_stitch_coordinates_and_dedupe_with_one_result():
    w,h=1080,589;im=np.zeros((h,w,3),np.uint8)
    regions=tile_regions(w,h,'2x2');counter=[0]
    global_box=[w*.495,h*.49,w*.505,h*.51]
    def fake(patch,cfg,*,purpose):
        i=counter[0];counter[0]+=1
        x0,y0,x1,y1=regions[i]
        # Center box is present in all 4 overlapping tiles.
        b=[(global_box[0]-x0)/(x1-x0)*1000,(global_box[1]-y0)/(y1-y0)*1000,
           (global_box[2]-x0)/(x1-x0)*1000,(global_box[3]-y0)/(y1-y0)*1000]
        return {'texts':[{'text':'NLRP3','bbox':b}],'warnings':[],'unpositioned':0,'usage':{'total_tokens':123}}
    out=recognize(im,AIConfig(api_key='local-test'),tiling='2x2',recognizer=fake)
    assert counter[0]==4
    assert out['request_count']==4 and out['estimated_ocr_cost_cny']==0.006
    assert len(out['texts'])==1
    assert abs(out['texts'][0]['bbox'][0]-495)<.1
    assert 'duplicate' in ' '.join(out['warnings'])


def test_overlapping_distinct_labels_not_deleted():
    same_box=[200,200,300,240]
    output,removed=_deduplicate([{'text':'NLRP3','bbox':same_box},{'text':'NLRP6','bbox':same_box}])
    assert len(output)==2 and removed==0


def test_review_artifacts_escape_text_and_retain_pixel_geometry(tmp_path):
    im=np.full((100,200,3),255,np.uint8)
    data={'texts':[{'text':'A <script>alert(1)</script>','bbox':[200,100,700,300]}],
          'request_count':1,'page_count':1,'warnings':[], 'tile_mode':'single'}
    paths=write_review_bundle(im,data,tmp_path)
    doc=json.loads(Path(paths['manifest']).read_text())
    assert doc['texts'][0]['bbox']==[40.,10.,140.,30.]
    assert doc['texts'][0]['position']==[40.,30.]
    assert doc['texts'][0]['reviewed'] is False
    html=Path(paths['html']).read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert Image.open(paths['overlay']).size==(200,100)


def test_rebuild_uses_approved_manifest_but_never_contacts_ai(tmp_path, monkeypatch):
    image=tmp_path/'source.png';Image.new('RGB',(280,100),'white').save(image)
    doc={'source_canvas':{'width':280,'height':100}, 'texts':[
        {'contents':'NLRP3','position':[10,38],'fontSize':16,
         'bbox':[10,12,100,40],'reviewed':True}]}
    mf=tmp_path/'manifest.json';mf.write_text(json.dumps(doc))
    import ai_bridge
    monkeypatch.setattr(ai_bridge,'_session',lambda: (_ for _ in ()).throw(AssertionError('Must stay offline')))
    out=tmp_path/'editable.pptx'
    result=convert(image,out,Settings(colors=4,max_shapes=300,manifest_clean=True),manifest_path=mf,
                   extra_svg=tmp_path/'editable.svg')
    assert result['texts']==1
    assert out.is_file()
    assert 'NLRP3' in [sh.text for sh in Presentation(out).slides[0].shapes if sh.has_text_frame]


def test_oversize_manifest_boxes_never_erase_whole_canvas(tmp_path):
    from engine import _manifest_box_mask
    image=np.ones((100,100,3),dtype=np.uint8)*255
    mf=tmp_path/'large.json'
    mf.write_text(json.dumps({'source_canvas':{'width':100,'height':100},
                              'texts':[{'contents':'BAD','bbox':[0,0,99,99]}]}))
    mask,warns=_manifest_box_mask(image,mf)
    assert not mask.any() and warns


def test_flash_review_structured_json_and_only_existing_ids(monkeypatch):
    # Stub transport rather than sending cloud traffic.
    import deepseek_workflow
    captured={}
    class Response:
        status_code=200
        content=b'{}'
        def json(self):
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'suggestions':[
                {'id':1,'text':'NLRP3'}, {'id':999,'text':'Made up'}, {'id':2,'text':'NLRC4'}]})}}]}
    class Sess:
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def post(self,url,**kw):
            captured.update({'url':url,'body':kw['json'],'auth':kw['headers']['Authorization']})
            return Response()
    monkeypatch.setattr(deepseek_workflow,'_session',lambda:Sess())
    data={'texts':[{'id':1,'contents':'NLRP8'},{'id':2,'contents':'NLRC4'}]}
    result=flash_review(np.ones((50,80,3),np.uint8),data,
                        AIConfig(base_url='https://api.deepseek.com',api_key='DUMMY'))
    assert len(result['suggestions'])==1
    assert captured['url']=='https://api.deepseek.com/chat/completions'
    assert captured['body']['response_format']=={'type':'json_object'}
    assert captured['body']['messages'][0]['content'][1]['image_url']['detail']=='original'
    assert 'DUMMY' not in json.dumps(result)


def test_flash_review_blank_json_fails_closed(monkeypatch):
    import deepseek_workflow
    class Resp:
        status_code=200
        content=b'{}'
        def json(self):return {'choices':[{'finish_reason':'stop','message':{'content':'   '}}]}
    class Sess:
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def post(self,*a,**k):return Resp()
    monkeypatch.setattr(deepseek_workflow,'_session',lambda:Sess())
    with pytest.raises(AIServiceError,match='empty'):
        flash_review(np.zeros((50,50,3),np.uint8),{'texts':[]},AIConfig(base_url='https://api.deepseek.com',api_key='X'))


def test_check_settings_blocks_incompatible_ocr_destination():
    from engine import check_settings
    with pytest.raises(ValueError,match='official'):
        check_settings(Settings(ai_mode='deepseek-ocr',ai_config=AIConfig(base_url='https://api.openai.com/v1',api_key='test')))
