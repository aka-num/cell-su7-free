#!/usr/bin/env python3
"""Opt-in loopback-only HTTP API for invoking Cell SU7 from local automation/Codex.

It NEVER accepts or serves the upstream API key. Set CELL_SU7_SERVER_TOKEN.
Third-party AI is blocked unless the process starts with --allow-remote-ai.
Requests reference existing files within --workspace; no file upload endpoint.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading

from ai_bridge import AIConfig
from app import make_config
from engine import Settings, convert

MAX_BODY = 16 * 1024


def _safe_path(workspace: Path, value: str, input_mode=False):
    if not isinstance(value,str) or not value.strip():
        raise ValueError('source/output must be a local file path')
    p=Path(value)
    if not p.is_absolute():p=workspace/p
    p=p.resolve()
    if not p.is_relative_to(workspace):
        raise ValueError('Source and output paths must stay inside the configured workspace')
    if input_mode and not p.is_file():
        raise FileNotFoundError('Input file does not exist inside workspace')
    return p


def make_handler(workspace: Path, token: str, allow_remote_ai=False):
    root=Path(workspace).resolve()
    if not root.is_dir():raise ValueError('Workspace must be an existing directory')
    if not token or len(token)<16:
        raise ValueError('Set CELL_SU7_SERVER_TOKEN to a random secret of at least 16 characters')
    busy=threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'CellSU7/3.0'
        sys_version = ''

        def log_message(self,*args):
            # Suppress path/query/header logging (which might expose user filenames).
            pass

        def send_json(self, status, content):
            payload=json.dumps(content,ensure_ascii=False,default=str).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers();self.wfile.write(payload)

        def _authorized(self):
            auth=self.headers.get('Authorization','')
            # Compare complete strings in constant time; reject other auth styles.
            return hmac.compare_digest(auth,'Bearer '+token)

        def do_GET(self):
            if self.path=='/health':
                self.send_json(200,{'ok':True,'service':'Cell SU7 local API','mode':'local-only',
                                    'remote_ai_enabled':allow_remote_ai})
            else:
                self.send_json(404,{'error':'not found'})

        def do_POST(self):
            if not self._authorized():
                self.send_json(401,{'error':'unauthorized'});return
            if self.path!='/v1/convert':
                self.send_json(404,{'error':'not found'});return
            try:
                content_type=self.headers.get('Content-Type','').split(';')[0].strip()
                if content_type!='application/json':
                    self.send_json(415,{'error':'Content-Type must be application/json'});return
                size=int(self.headers.get('Content-Length','-1'))
                if size<2 or size>MAX_BODY:
                    self.send_json(413,{'error':'request body is too large or missing'});return
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('JSON body must be an object')
                if not busy.acquire(blocking=False):
                    self.send_json(429,{'error':'another conversion is in progress'});return
            except (ValueError,TypeError) as e:
                self.send_json(400,{'error':'invalid request: '+str(e)[:100]});return
            try:
                source=_safe_path(root,data.get('source'),input_mode=True)
                output=_safe_path(root,data.get('output'))
                options=data.get('options',{})
                if not isinstance(options,dict):raise ValueError('options must be an object')
                mode=options.get('ai_mode','off')
                remote_clean=bool(options.get('ai_clean',False))
                if (mode!='off' or remote_clean) and not allow_remote_ai:
                    self.send_json(403,{'error':'AI is disabled for this server; restart with --allow-remote-ai'});return
                # A limited, documented allowlist. The request never supplies an API key or arbitrary API URL.
                s=Settings(colors=int(options.get('colors',16)),
                           backend=str(options.get('backend','auto')),
                           color_mode=str(options.get('color_mode','perceptual')),
                           structure_mode=str(options.get('structure_mode','hybrid')),
                           background_gradient=bool(options.get('background_gradient',True)),
                           ocr=bool(options.get('ocr',False)),
                           ocr_lang=str(options.get('ocr_lang','eng')),
                           inpaint=bool(options.get('inpaint',True)),
                           ai_mode=str(mode),ai_remote_clean=remote_clean,
                           ai_accept_changes=bool(options.get('ai_accept_changes',False)),
                           ai_config=make_config({}))
                extra_svg=_safe_path(root,options['svg_out']) if 'svg_out' in options else None
                manifest=_safe_path(root,options['manifest'],input_mode=True) if 'manifest' in options else None
                manifest_out=_safe_path(root,options['manifest_out']) if 'manifest_out' in options else None
                quality=_safe_path(root,options['report']) if 'report' in options else None
                res=convert(source,output,s,manifest,extra_svg,manifest_output=manifest_out,report_path=quality)
                self.send_json(200,{'ok':True,'result':res})
            except (ValueError,TypeError,FileNotFoundError) as e:
                self.send_json(422,{'error':str(e)[:500]})
            except Exception:
                # Avoid sending provider response details or tracebacks in API errors.
                self.send_json(500,{'error':'Conversion failed; inspect local application environment'})
            finally:busy.release()

    return Handler


def main():
    p=argparse.ArgumentParser(description='Loopback-only local HTTP conversion server')
    p.add_argument('--host',default='127.0.0.1',choices=['127.0.0.1','localhost'])
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--workspace',type=Path,default=Path.cwd())
    p.add_argument('--allow-remote-ai',action='store_true',help='Explicitly allow cost-incurring AI tasks')
    a=p.parse_args()
    token=os.environ.get('CELL_SU7_SERVER_TOKEN','')
    handler=make_handler(a.workspace,token,a.allow_remote_ai)
    srv=ThreadingHTTPServer((a.host,a.port),handler)
    print(f'Cell SU7 local API listening on http://{a.host}:{srv.server_port} ; workspace={a.workspace.resolve()}',flush=True)
    print('GET /health (public locally); POST /v1/convert requires CELL_SU7_SERVER_TOKEN',flush=True)
    try:srv.serve_forever()
    except KeyboardInterrupt:pass
    finally:srv.server_close()

if __name__=='__main__':main()
