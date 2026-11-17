"""Transport failures are actionable and do not reveal credentials or auto-resubmit."""
from __future__ import annotations
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import pytest
import requests

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ai_bridge import (AIConfig, _ocr_network_message, diagnose_deepseek_connection)

@pytest.mark.parametrize('error,term',[ 
    (requests.exceptions.ConnectTimeout('mock'), '建立连接超时'),
    (requests.exceptions.ReadTimeout('mock'), '等待响应超时'),
    (requests.exceptions.ProxyError('mock'), '代理连接失败'),
    (requests.exceptions.SSLError('mock'), '证书验证失败'),
    (requests.exceptions.ConnectionError('Failed to resolve host'), 'DNS 解析失败'),
    (requests.exceptions.ConnectionError('connection reset'), '网络连接中断'),
])
def test_ocr_network_error_category(error,term):
    message = _ocr_network_message(error)
    assert term in message
    assert 'sk-secret-test' not in message

@contextmanager
def fake_models_server():
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            received.append((self.path,self.headers.get('Authorization')))
            status=200 if self.headers.get('Authorization')=='Bearer TEST-KEY' else 401
            self.send_response(status)
            self.send_header('Content-Length','0')
            self.end_headers()
    srv=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=threading.Thread(target=srv.serve_forever,daemon=True)
    t.start()
    try: yield srv.server_port,received
    finally:srv.shutdown();srv.server_close();t.join(timeout=4)


def test_no_charge_check_does_not_send_key_or_post(monkeypatch):
    # Redirect only the trusted fixed /models diagnostic target to localhost.
    import ai_bridge
    real_session=ai_bridge._session
    class RewritingSession:
        def __init__(self,port): self.port=port;self.inner=real_session()
        def __enter__(self):return self
        def __exit__(self,*args):self.inner.close()
        def get(self,url,**kwargs):
            assert url=='https://api.deepseek.com/models'
            return self.inner.get(f'http://127.0.0.1:{self.port}/models',**kwargs)
    with fake_models_server() as (port,seen):
        monkeypatch.setattr(ai_bridge,'_session',lambda:RewritingSession(port))
        config=AIConfig(base_url='https://api.deepseek.com',api_key='TEST-KEY')
        out=diagnose_deepseek_connection(config)
        assert out['http_status']==401
        assert seen==[('/models',None)]
        out=diagnose_deepseek_connection(config,check_auth=True)
        assert out['http_status']==200
        assert seen[-1]==('/models','Bearer TEST-KEY')


def test_diag_does_not_call_wrong_provider():
    with pytest.raises(ValueError,match='api.deepseek.com'):
        diagnose_deepseek_connection(AIConfig(base_url='https://example.org'))
