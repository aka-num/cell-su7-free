"""Offline integration tests of the documented DeepSeek multipart OCR interface.

No credential or request is ever sent to the public DeepSeek endpoint here.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from pptx import Presentation
import numpy as np
import pytest

from ai_bridge import AIConfig, AIServiceError, parse_deepseek_ocr, deepseek_ocr_image, _deepseek_ocr_url
from engine import Settings, convert, check_settings

SAMPLE = '<｜ref｜>NLRP3<｜/ref｜><｜box｜>[[230,350,320,390]]<｜/box｜>\n<｜ref｜>Staphylococcus aureus<｜/ref｜><｜box｜>[[420,15,610,40]]<｜/box｜>'

@contextmanager
def mock_ocr_server(ocr_text=SAMPLE, status=200):
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            received.append({'path':self.path,'auth':self.headers.get('Authorization'),
                             'content_type':self.headers.get('Content-Type'),'body':body})
            reply={'object':'ocr.result','purpose':'text_recognition','page_count':1,
                   'pages':[{'index':0,'text':ocr_text,'finish_reason':'stop'}],
                   'usage':{'total_tokens':121}}
            data=json.dumps(reply).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data)
    srv=ThreadingHTTPServer(('127.0.0.1',0), Handler)
    t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
    try:yield f'http://127.0.0.1:{srv.server_port}',received
    finally:srv.shutdown();srv.server_close();t.join(timeout=5)

def test_url_builds_official_and_proxy_endpoints():
    assert _deepseek_ocr_url(AIConfig(base_url='https://api.deepseek.com')) == 'https://api.deepseek.com/v1/tools/ocr'
    assert _deepseek_ocr_url(AIConfig(base_url='https://api.deepseek.com/v1')) == 'https://api.deepseek.com/v1/tools/ocr'

def test_extract_boxes_without_inventing_text():
    data={'pages':[{'index':0,'text':SAMPLE+'\nSome unboxed prose'}]}
    out=parse_deepseek_ocr(data)
    assert [a['text'] for a in out['texts']]==['NLRP3','Staphylococcus aureus']
    assert out['texts'][0]['bbox']==[230,350,320,390]
    bad='<｜ref｜>malicious<｜/ref｜><｜box｜>[[-20,10,99,99]]<｜/box｜>'
    assert not parse_deepseek_ocr({'pages':[{'text':bad}]})['texts']
    with pytest.raises(AIServiceError):parse_deepseek_ocr({'not_pages':[]})

def test_actual_multipart_request_to_local_simulator():
    im=np.asarray(Image.new('RGB',(100,60),'white'))
    with mock_ocr_server() as (url,received):
        out=deepseek_ocr_image(im,AIConfig(base_url=url,api_key='LOCAL-TEST-KEY'))
        assert len(out['texts'])==2
        assert out['usage']['total_tokens']==121
        assert len(received)==1
        req=received[0]
        assert req['path']=='/v1/tools/ocr'
        assert req['auth']=='Bearer LOCAL-TEST-KEY'
        assert req['content_type'].startswith('multipart/form-data')
        assert b'name="purpose"' in req['body'] and b'text_recognition' in req['body']
        assert b'name="file"' in req['body'] and b'\x89PNG\r\n' in req['body']

def test_full_pathogen_image_rebuild_with_mock_model(tmp_path):
    source=Path(__file__).resolve().parents[1]/'examples'/'demo_scientific.png'
    assert source.exists()
    with mock_ocr_server() as (url,received):
        settings=Settings(ai_mode='deepseek-ocr', ai_config=AIConfig(base_url=url,api_key='LOCAL-TEST-KEY'),
                          colors=8, max_shapes=700, backend='contour')
        out=tmp_path/'deepseek-ocr.pptx'
        stats=convert(source,out,settings,extra_svg=tmp_path/'deepseek-ocr.svg',
                      manifest_output=tmp_path/'text.json')
        assert stats['texts']==2
        assert stats['ai']['mode']=='deepseek-ocr'
        assert len(received)==1
        labels=[sh.text for sh in Presentation(out).slides[0].shapes if sh.has_text_frame and sh.text]
        assert 'NLRP3' in labels
        assert (tmp_path/'deepseek-ocr.svg').exists()
        manifest=(tmp_path/'text.json').read_text()
        assert 'Staphylococcus aureus' in manifest
        assert 'LOCAL-TEST-KEY' not in manifest

def test_deepseek_no_unsupported_remote_edit():
    c=AIConfig(base_url='https://api.deepseek.com',api_key='NOT-USED')
    with pytest.raises(ValueError,match='does not provide'):
        check_settings(Settings(ai_mode='deepseek-ocr',ai_config=c,ai_remote_clean=True))
