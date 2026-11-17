"""Test real HTTP request construction against a local fake API; never charge a vendor."""
from __future__ import annotations
import base64
from contextlib import contextmanager
import io
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from PIL import Image,ImageDraw
from pptx import Presentation
import pytest
import ai_bridge
from ai_bridge import AIConfig,AIServiceError,analyze_image,edit_remove_text,probe_provider,save_profile,load_profile,validated_analysis,check_clean_image
from engine import Settings,convert,create_scene


@contextmanager
def fake_provider(response=None,edit_image=None,status=200):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def _send(self,payload,code=200):
            blob=json.dumps(payload).encode('utf8')
            self.send_response(code);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob)
        def do_GET(self):
            requests.append(('GET',self.path,self.headers.get('Authorization',''),None))
            self._send({'data':[]},status)
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            requests.append(('POST',self.path,self.headers.get('Authorization',''),body))
            if self.path.endswith('/chat/completions'):
                answer=response or {'texts':[{'text':'HELLO','bbox':[100,150,450,330],'color':'#111111'}]}
                self._send({'choices':[{'message':{'content':json.dumps(answer)}}]},status)
            elif self.path.endswith('/images/edits'):
                img=edit_image if edit_image is not None else Image.new('RGB',(120,80),'white')
                b=io.BytesIO();img.save(b,'PNG')
                self._send({'data':[{'b64_json':base64.b64encode(b.getvalue()).decode()}]},status)
            else:self._send({'error':'unsupported'},404)
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
    try:
        yield 'http://127.0.0.1:%d/v1'%srv.server_port,requests
    finally:srv.shutdown();srv.server_close();t.join(timeout=3)


def sample_image(tmp_path):
    im=Image.new('RGB',(120,80),'white');d=ImageDraw.Draw(im)
    d.text((13,13),'HELLO',fill='black')
    src=tmp_path/'figure.png';im.save(src)
    return src,np.asarray(im)


def test_profile_never_saves_secret(tmp_path):
    c=AIConfig(base_url='https://api.example.org/v1',api_key='SENSITIVE-DO-NOT-SAVE')
    f=tmp_path/'private'/'profile.json'
    save_profile(c,f)
    content=f.read_text()
    assert 'SENSITIVE' not in content and 'api_key' not in content
    assert load_profile(f)['base_url']=='https://api.example.org/v1'
    assert load_profile(f).get('api_key') is None
    f.write_text('{"api_key":"WILL-NOT-LOAD","vision_model":"vision"}')
    assert 'api_key' not in load_profile(f)


def test_security_rejects_unencrypted_nonlocal_and_inline_secret():
    for url in ('http://remote.tld/v1','https://alice:password@remote.tld/v1',
                'https://remote.tld/v1?key=unsafe','https://remote.tld/v1/chat/completions'):
        with pytest.raises(ValueError):AIConfig(base_url=url).validate()
    AIConfig(base_url='http://localhost:11434/v1').validate()


def test_ai_vision_real_local_http_and_ppt_export(tmp_path,monkeypatch):
    src,img=sample_image(tmp_path)
    with fake_provider() as (url,requests):
        c=AIConfig(base_url=url,api_key='TEST-LOCAL-TOKEN',vision_model='test-vision')
        assert probe_provider(c).startswith('API connected')
        analysis=analyze_image(img,c,'ocr')
        assert analysis['texts'][0]['text']=='HELLO'
        scene,info=create_scene(src,Settings(backend='contour',ai_mode='ocr',ai_config=c,colors=3))
        assert len(scene.texts)==1
        assert info['ai']['mode']=='ocr'
        out=tmp_path/'edit.pptx'
        convert(src,out,Settings(backend='contour',ai_mode='ocr',ai_config=c,colors=3),
                manifest_output=tmp_path/'manifest.json')
        assert len(Presentation(out).slides[0].shapes)>0
        assert (tmp_path/'manifest.json').exists()
        assert any('TEST-LOCAL-TOKEN' in r[2] for r in requests)
        assert all('TEST-LOCAL-TOKEN' not in str(f.read_text(errors='replace')) for f in [tmp_path/'manifest.json'])
        req=next(r for r in requests if r[1]=='/v1/chat/completions')
        payload=json.loads(req[3]);assert payload['model']=='test-vision'
        data_url=payload['messages'][0]['content'][1]['image_url']['url']
        assert data_url.startswith('data:image/png;base64,')
        assert len(base64.b64decode(data_url.split(',',1)[1]))>100


def test_ai_semantic_layout_editable_native_shapes(tmp_path):
    src,_=sample_image(tmp_path)
    semantic={'texts':[{'text':'ALPHA','bbox':[250,250,700,400]}],
              'shapes':[{'kind':'rectangle','bbox':[100,100,900,900], 'fill':'#aabbcc','stroke':'#000000'},
                        {'kind':'arrow','points':[[100,500],[900,500]],'stroke':'#ff0000','stroke_width':3}]}
    with fake_provider(response=semantic) as (url,requests):
        s=Settings(ai_mode='layout',ai_config=AIConfig(base_url=url,api_key='KEY'))
        output=tmp_path/'semantic.pptx';svg=tmp_path/'semantic.svg'
        result=convert(src,output,s,extra_svg=svg)
        assert result['backend']=='ai_layout' and result['shapes']==4 and result['texts']==1
        ppt=Presentation(output)
        assert len(ppt.slides[0].shapes)==5
        assert any(s.text=='ALPHA' for s in ppt.slides[0].shapes if s.has_text_frame)
        assert svg.exists()
        assert len(requests)==1


def test_ocr_and_manifest_do_not_duplicate_charge(tmp_path):
    src,_=sample_image(tmp_path)
    manifest=tmp_path/'curated.json'
    manifest.write_text(json.dumps({'texts':[{'contents':'MANUAL','position':[20,30]}]}))
    s=Settings(ai_mode='ocr',ai_config=AIConfig(base_url='http://127.0.0.1:18080/v1',api_key='NONE'))
    with pytest.raises(ValueError,match='curated manifest'):
        create_scene(src,s,manifest)
    with pytest.raises(ValueError,match='Choose local OCR'):
        create_scene(src,Settings(ocr=True,ai_mode='ocr',ai_config=s.ai_config))


def test_ai_image_edits_sends_mask_and_handles_changes(tmp_path):
    src,img=sample_image(tmp_path)
    full_black=Image.new('RGB',(120,80),'black')
    mask=np.zeros((80,120),dtype=np.uint8);mask[15:30,15:50]=255
    with fake_provider(edit_image=full_black) as (url,requests):
        edited,usage=edit_remove_text(img,mask,AIConfig(base_url=url,api_key='TEST'))
        assert edited.shape==img.shape
        edited,mae,ok=check_clean_image(img,edited,mask)
        assert not ok and mae>100
        req=next(r for r in requests if r[1]=='/v1/images/edits')
        assert b'name="mask"' in req[3]
        assert b'name="image"' in req[3]
        assert b'name="model"' in req[3]


def test_remote_clean_guard_rejects_changed_geometry(tmp_path,monkeypatch):
    src,_=sample_image(tmp_path)
    monkeypatch.setattr(ai_bridge,'edit_remove_text',lambda img,mask,c:(np.zeros_like(img),{}))
    with fake_provider() as (url,requests):
        s=Settings(backend='contour',colors=3,ai_mode='ocr',ai_config=AIConfig(base_url=url,api_key='TEST'),
                   ai_remote_clean=True,clean_preview=str(tmp_path/'clean.png'))
        scene,info=create_scene(src,s)
        assert info['ai']['edit_accepted']==False
        assert 'local inpainting' in ' '.join(info['warnings'])
        assert (tmp_path/'clean.png').exists()
        assert np.array(Image.open(tmp_path/'clean.png')).mean()>200
        s.ai_accept_changes=True
        scene,info=create_scene(src,s)
        assert info['ai']['edit_accepted']==True
        assert np.asarray(Image.open(tmp_path/'clean.png')).mean()==0


def test_invalid_reply_redacts_key(tmp_path):
    _,img=sample_image(tmp_path)
    with fake_provider(status=401) as (url,requests):
        key='NEVER-EXPOSE-ME'
        with pytest.raises(AIServiceError) as caught:
            analyze_image(img,AIConfig(base_url=url,api_key=key))
        assert key not in str(caught.value)


def test_local_mode_does_not_contact_provider(tmp_path,monkeypatch):
    src,_=sample_image(tmp_path)
    def raise_if_called(*a,**k):
        raise AssertionError('No network should happen in local mode')
    monkeypatch.setattr(ai_bridge,'analyze_image',raise_if_called)
    monkeypatch.setattr(ai_bridge,'edit_remove_text',raise_if_called)
    s=Settings(backend='contour',colors=3,ocr=False)
    output=tmp_path/'local.pptx'
    result=convert(src,output,s)
    assert result['source_type']=='raster'
    assert 'ai' not in result and output.exists()


def test_ai_input_ignores_untrusted_malformed_boxes():
    result=validated_analysis({'texts':[{'text':'bad','bbox':[-2,0,100,10]},
                                                {'text':'good','bbox':[10,20,900,700]}],
                               'shapes':[{'kind':'malware','points':[[1,1],[2,2]]}]},'layout')
    assert len(result['texts'])==1 and result['texts'][0]['text']=='good'
    assert not result['shapes']


def test_input_not_overwritten_and_no_ai_charge_before_path_checks(tmp_path,monkeypatch):
    src,img=sample_image(tmp_path)
    before=src.read_bytes()
    def forbidden(*args,**kwargs):raise AssertionError('AI API must not be called on invalid outputs')
    monkeypatch.setattr(ai_bridge,'analyze_image',forbidden)
    c=AIConfig(base_url='http://127.0.0.1:12345/v1',api_key='KEY')
    s=Settings(ai_mode='ocr',ai_config=c)
    with pytest.raises(ValueError,match='overwrite'):
        convert(src,src,s)
    with pytest.raises(ValueError,match='Output must'):
        convert(src,tmp_path/'invalid.txt',s)
    with pytest.raises(ValueError,match='overwrite'):
        convert(src,tmp_path/'result.pptx',s,manifest_output=src)
    assert src.read_bytes()==before


def test_hybrid_uses_local_coordinates_and_ai_transcription():
    from engine import Text,_hybrid_text_reconcile
    loc=[Text('HE1LO',20,45,18,(10,10,10))]
    vis=[Text('HELLO',22,44,20,(100,100,100))]
    out,changed=_hybrid_text_reconcile(loc,vis,250,100)
    assert changed==1 and out[0].content=='HELLO'
    assert out[0].x==loc[0].x and out[0].y==loc[0].y and out[0].color==loc[0].color
    far=[Text('NOT PRESENT',230,90,25)]
    out,changed=_hybrid_text_reconcile(loc,far,250,100)
    assert changed==0 and out[0].content=='HE1LO'
