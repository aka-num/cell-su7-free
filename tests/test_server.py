"""End-to-end secured local-HTTP API tests."""
from __future__ import annotations
import io
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from PIL import Image
import pytest
from server import make_handler


@pytest.fixture
def local_api(tmp_path):
    token='x'*32
    httpd=ThreadingHTTPServer(('127.0.0.1',0),make_handler(tmp_path,token))
    thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{httpd.server_port}',tmp_path,token
    finally:httpd.shutdown();httpd.server_close();thread.join(timeout=2)


def post(url,body,token=None):
    data=json.dumps(body).encode()
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    req=urllib.request.Request(url+'/v1/convert',data=data,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=10) as rsp:return rsp.status,json.loads(rsp.read())
    except urllib.error.HTTPError as e:return e.code,json.loads(e.read())


def test_requires_token_and_restricts_workspace(local_api,tmp_path):
    base,root,token=local_api
    image=Image.new('RGB',(90,70),'white');image.save(root/'source.png')
    body={'source':'source.png','output':'output.pptx','options':{'backend':'contour'}}
    code,result=post(base,body)
    assert code==401
    code,result=post(base,body,'incorrect'*4)
    assert code==401
    code,result=post(base,body,token)
    assert code==200 and result['ok']
    assert (root/'output.pptx').exists()
    body['output']='../escape.pptx'
    code,result=post(base,body,token)
    assert code==422
    assert not (root.parent/'escape.pptx').exists()


def test_remote_ai_disabled_by_default(local_api):
    base,root,token=local_api
    Image.new('RGB',(50,50),'white').save(root/'src.png')
    code,result=post(base,{'source':'src.png','output':'out.pptx','options':{'ai_mode':'ocr'}},token)
    assert code==403 and 'disabled' in result['error']
    assert not (root/'out.pptx').exists()


def test_serve_health_without_exposing_token(local_api):
    base,root,token=local_api
    with urllib.request.urlopen(base+'/health') as rsp:result=json.load(rsp)
    assert result['mode']=='local-only'
    assert not result['remote_ai_enabled']
    assert token not in str(result)


def test_server_requires_strong_token(tmp_path):
    with pytest.raises(ValueError,match='at least 16'):
        make_handler(tmp_path,'tiny')
