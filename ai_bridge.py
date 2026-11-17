"""Explicit, optional, OpenAI-compatible AI assistance for Cell SU7.

No network activity at import time; every request requires an explicit AI mode.
API keys are NEVER included in saved JSON settings, logs, file names, or reports.
Not every OpenAI-compatible server supports image input or image editing.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import numpy as np
from PIL import Image

DEFAULT_URL = "https://api.openai.com/v1"
PROFILE_FILE = Path.home() / ".config" / "cell_su7_remastered" / "ai-profile.json"
KEYRING_SERVICE = "cell-su7-remastered-ai"
MAX_AI_IMAGE = 16 * 1024 * 1024
MAX_REPLY = 30 * 1024 * 1024


class AIServiceError(RuntimeError):
    """An error intentionally safe to show without exposing credentials."""


def validated_url(base_url: str) -> str:
    """Validate a trusted OpenAI-compatible API base URL (not a request URL)."""
    base_url = base_url.strip().rstrip("/")
    u = urlsplit(base_url)
    if not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("API Base URL must be an origin/path without credentials, query or fragment")
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname.lower() in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Use HTTPS for remote APIs; HTTP is allowed only for localhost")
    if u.path.endswith(("/chat/completions", "/images/edits", "/models")):
        raise ValueError("Enter the API base URL (typically ending /v1), not an endpoint")
    if re.search(r"[\s\x00-\x1f]", base_url):
        raise ValueError("Invalid API Base URL")
    return base_url


@dataclass(frozen=True)
class AIConfig:
    base_url: str = DEFAULT_URL
    vision_model: str = "gpt-4.1-mini"
    edit_model: str = "gpt-image-2.5-flare"
    api_key: str = ""
    key_env: str = "CELL_SU7_API_KEY"
    timeout: float = 120.0

    def validate(self):
        validated_url(self.base_url)
        if not self.vision_model.strip() or not self.edit_model.strip():
            raise ValueError("Vision model and edit model must not be empty")
        if not 3 <= self.timeout <= 600:
            raise ValueError("AI timeout must be between 3 and 600 seconds")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key_env):
            raise ValueError("Invalid API key environment variable name")
        return self

    def key(self) -> str:
        """Explicit argument, then environment, then optional OS credential storage."""
        key = self.api_key.strip() or os.environ.get(self.key_env, "").strip()
        if key:
            return key
        try:
            import keyring
            return (keyring.get_password(KEYRING_SERVICE, validated_url(self.base_url)) or "").strip()
        except Exception:
            return ""


def save_profile(config: AIConfig, path: Path = PROFILE_FILE):
    """Persist ONLY non-secret options. Opt-in storage, never plaintext API keys."""
    config.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {
        "base_url": validated_url(config.base_url),
        "vision_model": config.vision_model,
        "edit_model": config.edit_model,
        "key_env": config.key_env,
        "timeout": config.timeout,
    }
    path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_profile(path: Path = PROFILE_FILE) -> dict:
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid AI profile")
    # Allowlist only; in particular never read a key from on-disk JSON.
    return {k: data[k] for k in ("base_url", "vision_model", "edit_model", "key_env", "timeout") if k in data}


def store_key(config: AIConfig):
    """Only called after the user explicitly clicks 'store key' (requires keyring)."""
    if not config.api_key.strip():
        raise ValueError("Enter the API key to store")
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, validated_url(config.base_url), config.api_key.strip())
    except Exception as e:
        raise AIServiceError("OS credential storage unavailable. Install and configure keyring, or use an environment variable.") from e


def forget_key(config: AIConfig):
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, validated_url(config.base_url))
    except ImportError as e:
        raise AIServiceError("Install keyring to manage stored credentials") from e
    except Exception as e:
        raise AIServiceError("Could not remove the stored credential") from e


def _session():
    try:
        import requests
    except ImportError as e:
        raise AIServiceError("Install optional AI support: pip install -r requirements-ai.txt") from e
    return requests.Session()


def _read_response(response):
    """Bounded response parsing; never echo response bodies or request headers."""
    status = response.status_code
    if status >= 400:
        if status == 401:
            raise AIServiceError("AI service rejected authentication (HTTP 401). Check your key.")
        if status == 402:
            raise AIServiceError("AI service reports insufficient balance (HTTP 402).")
        if status == 403:
            raise AIServiceError("AI service denied access (HTTP 403). Check model permissions.")
        if status == 429:
            raise AIServiceError("AI service rate limit or insufficient quota (HTTP 429).")
        raise AIServiceError(f"AI service returned HTTP {status}; check provider endpoint and model compatibility.")
    if len(response.content) > MAX_REPLY:
        raise AIServiceError("AI service response is too large")
    try:
        data = response.json()
    except ValueError as e:
        raise AIServiceError("AI service did not return JSON") from e
    if not isinstance(data, dict):
        raise AIServiceError("AI service returned an invalid JSON object")
    return data


def _credentials(config):
    config.validate()
    key = config.key()
    if not key:
        raise AIServiceError(f"AI key is missing. Enter it in Settings or set {config.key_env}.")
    return {"Authorization": "Bearer " + key}


def _endpoint(config, suffix):
    return validated_url(config.base_url) + suffix


def probe_provider(config: AIConfig) -> str:
    """Test GET /models. Some compatible providers legitimately do not expose it."""
    headers = _credentials(config)
    try:
        with _session() as sess:
            r = sess.get(_endpoint(config, "/models"), headers=headers, timeout=(10, min(config.timeout, 30)), allow_redirects=False)
        if 300 <= r.status_code < 400:
            raise AIServiceError("AI server redirected /models; redirect blocked to protect your key")
        _read_response(r)
        return "API connected (/models). Vision/edit capabilities still depend on the selected models."
    except AIServiceError:
        raise
    except Exception as e:
        raise AIServiceError("Could not reach the AI endpoint. Check the URL, network, and certificate.") from e


def _image_data_url(rgb_image: np.ndarray, max_edge: int = 1600) -> str:
    im = Image.fromarray(rgb_image.astype(np.uint8)).convert("RGB")
    w, h = im.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        im = im.resize((max(1, round(w*scale)), max(1, round(h*scale))), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    im.save(buffer, format="PNG", optimize=True)
    if buffer.tell() > MAX_AI_IMAGE:
        raise ValueError("AI input image too large")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise AIServiceError("Vision model did not return structured JSON")
        try:
            value = json.loads(text[start:end+1])
        except json.JSONDecodeError as e:
            raise AIServiceError("Vision model response could not be parsed as JSON") from e
    if not isinstance(value, dict):
        raise AIServiceError("Vision response must be a JSON object")
    return value


def _validate_bbox(box):
    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        x0,y0,x1,y1 = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if not all(0 <= a <= 1000 for a in (x0, y0, x1, y1)) or x1<=x0 or y1<=y0:
        return None
    return [x0,y0,x1,y1]


def validated_analysis(data, mode: str) -> dict:
    """Keep output bounded and deterministic; model suggestions are untrusted."""
    words = data.get("texts", [])
    shapes = data.get("shapes", []) if mode == "layout" else []
    if not isinstance(words, list) or not isinstance(shapes, list):
        raise AIServiceError("AI layout schema must contain lists of texts and shapes")
    text_out, shape_out = [], []
    for item in words[:300]:
        if not isinstance(item, dict):
            continue
        content = item.get("text", "")
        box = _validate_bbox(item.get("bbox"))
        if not isinstance(content, str) or not content.strip() or len(content)>300 or not box:
            continue
        text_out.append({"text": content.strip(), "bbox": box, "color": item.get("color", "#101820") if isinstance(item.get("color", "#101820"), str) else "#101820", "confidence": float(item.get("confidence", 0.7)) if isinstance(item.get("confidence", 0.7), (int,float)) else 0.7})
    for item in shapes[:300]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).lower()
        if kind not in ("rectangle", "ellipse", "line", "polyline", "arrow"):
            continue
        box = _validate_bbox(item.get("bbox"))
        pts = item.get("points", [])
        if not box and kind in ("rectangle", "ellipse"):
            continue
        if kind in ("line", "polyline", "arrow"):
            if not isinstance(pts, list) or len(pts)<2 or len(pts)>150:
                continue
            try:
                pts = [[float(p[0]), float(p[1])] for p in pts]
            except (ValueError, TypeError, IndexError):
                continue
            if not all(0 <= p[0] <= 1000 and 0 <= p[1] <= 1000 for p in pts):
                continue
        def color(name, fallback):
            v = item.get(name, fallback)
            return v[:40] if isinstance(v, str) else fallback
        def scalar(name, fallback):
            v=item.get(name, fallback)
            return max(0., min(40., float(v))) if isinstance(v, (int,float)) and np.isfinite(v) else fallback
        shape_out.append({"kind":kind, "bbox":box, "points":pts, "fill":color("fill","none"), "stroke":color("stroke","#111111"), "stroke_width":scalar("stroke_width",2.)})
    return {"texts":text_out, "shapes":shape_out}


def analyze_image(img: np.ndarray, config: AIConfig, mode: str = "ocr") -> dict:
    """A single user-authorized multimodal Chat Completions call. Sends the source image."""
    if mode not in ("ocr", "layout"):
        raise ValueError("AI mode must be 'ocr' or 'layout'")
    headers = _credentials(config)
    headers["Content-Type"] = "application/json"
    if mode == "ocr":
        prompt = (
            "Transcribe ONLY visibly present text from this scientific diagram. Return JSON only: "
            '{"texts":[{"text":"exact glyphs","bbox":[left,top,right,bottom],"color":"#RRGGBB","confidence":0.9}]}. '
            "Coordinates are independently normalized to 0..1000 for x and y, measured from the TOP LEFT. "
            "Do not invent characters, labels, or diagram shapes. Keep groups to one readable line. "
            "Include rotated and CJK labels if legible. Use [] when unsure."
        )
    else:
        prompt = (
            "Reconstruct this SCIENTIFIC DIAGRAM as editable semantic objects, not a generic image. "
            "Return JSON only with keys texts and shapes. "
            'texts:[{"text":"exact text","bbox":[x0,y0,x1,y1],"color":"#RRGGBB","confidence":0.9}]; '
            'shapes:[{"kind":"rectangle|ellipse|line|polyline|arrow","bbox":[x0,y0,x1,y1],'
            '"points":[[x,y],...],"fill":"#RRGGBB|none","stroke":"#RRGGBB|none","stroke_width":2}]. '
            "All coordinates normalized 0..1000 independently per image axis. Shapes ordered BACK to FRONT. "
            "Only include clean obvious chart primitives, axes, arrows, and solid blocks. Do not hallucinate. "
            "For line/polyline/arrow supply points, for rectangle/ellipse supply bbox. "
            "Every piece of visible text belongs to texts, not a path."
        )
    body = {"model":config.vision_model, "messages":[
        {"role":"user", "content":[{"type":"text","text":prompt},
                                     {"type":"image_url","image_url":{"url":_image_data_url(img),"detail":"original"}}]}
    ]}
    if urlsplit(validated_url(config.base_url)).hostname == 'api.deepseek.com':
        # Official JSON Output; other compatible vendors may not implement it.
        body['response_format']={'type':'json_object'}
        body['max_tokens']=8192
    try:
        with _session() as sess:
            r = sess.post(_endpoint(config,"/chat/completions"),headers=headers,json=body,
                          timeout=(10,config.timeout),allow_redirects=False)
        if 300 <= r.status_code < 400:
            raise AIServiceError("AI API redirect blocked to protect your key")
        response=_read_response(r)
    except AIServiceError:
        raise
    except Exception as e:
        raise AIServiceError("AI vision request failed (network/timeout/TLS). Check the endpoint.") from e
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise AIServiceError("DeepSeek vision JSON was truncated; no shapes were applied")
        content = choice["message"]["content"]
        if not content or (isinstance(content,str) and not content.strip()):
            raise AIServiceError("Vision provider returned blank JSON content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p,dict))
        parsed = _extract_json(content)
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise AIServiceError("Incompatible Chat Completions response from provider") from e
    validated = validated_analysis(parsed,mode)
    validated["model"] = config.vision_model
    return validated



# DeepSeek dedicated OCR: official schema 2026-11, normalized 0-1000 boxes.
# Text and coordinates are both UNTRUSTED. Never infer geometry for unboxed prose.
_DEEPSEEK_BOX = re.compile(
    r'<[|｜]ref[|｜]>(.*?)<[|｜]/ref[|｜]>\s*'
    r'<[|｜]box[|｜]>(.*?)<[|｜]/box[|｜]>', re.DOTALL | re.IGNORECASE,
)
_DEEPSEEK_REF = re.compile(r'<[|｜]ref[|｜]>(.*?)<[|｜]/ref[|｜]>',re.DOTALL | re.IGNORECASE)


def _deepseek_ocr_url(config: AIConfig) -> str:
    base = validated_url(config.base_url)
    # Base can be the origin or the /v1 path. Custom loopback mock is allowed.
    return base + ('/tools/ocr' if base.endswith('/v1') else '/v1/tools/ocr')


def _parse_ocr_boxes(payload: str) -> list[list[float]]:
    try:
        value=json.loads(payload.strip())
    except (ValueError,TypeError):
        return []
    if not isinstance(value,list):
        return []
    if len(value)==4 and all(isinstance(x,(int,float)) and not isinstance(x,bool) for x in value):
        value=[value]
    boxes=[]
    for box in value:
        if isinstance(box,list):
            b=_validate_bbox(box)
            if b is not None:boxes.append(b)
    return boxes


def parse_deepseek_ocr(data:dict, *, allow_partial:bool=False) -> dict:
    """Parse official OCR result; don't manufacture positions or confidence.

    DeepSeek OCR sends boxes embedded in each `pages[].text` string, not a
    structured `pages[].boxes` array. `finish_reason=length` means incomplete.
    """
    if not isinstance(data,dict) or data.get('error'):
        raise AIServiceError('DeepSeek OCR returned an error instead of an OCR result')
    pages=data.get('pages')
    if not isinstance(pages,list) or not pages:
        raise AIServiceError('DeepSeek OCR response has no pages')
    if len(pages)!=1:
        raise AIServiceError('This image editor supports one OCR image page, not multi-page documents')
    page=pages[0]
    if not isinstance(page,dict) or not isinstance(page.get('text'),str):
        raise AIServiceError('Invalid DeepSeek OCR page')
    if page.get('index',0)!=0:
        raise AIServiceError('Unexpected OCR page index')
    finish=page.get('finish_reason')
    # The official API calls this a partial *page*, not a failed request.
    # Preserve only fully closed ref/box pairs and require review. Never
    # automatically resubmit an already potentially billable OCR request.
    if finish=='length':
        allow_partial=True
    if finish not in ('stop','length',None):
        raise AIServiceError('DeepSeek OCR ended without a complete result')
    raw=page['text']
    if len(raw)>2_000_000:
        raise AIServiceError('DeepSeek OCR result exceeds safe text limit')
    recognized=[];warnings=[];bad_boxes=0;merged=0
    for match in _DEEPSEEK_BOX.finditer(raw):
        content=' '.join(match.group(1).split()).strip()
        boxes=_parse_ocr_boxes(match.group(2))
        if not content or len(content)>300 or not boxes:
            bad_boxes+=1
            continue
        # Multi-box values may span disconnected regions: use enclosing bounds,
        # flag for review. Do not present as pixel-perfect alignment.
        if len(boxes)>1:
            merged+=1
        box=[min(v[0] for v in boxes),min(v[1] for v in boxes),
             max(v[2] for v in boxes),max(v[3] for v in boxes)]
        if not _validate_bbox(box):
            bad_boxes+=1
            continue
        recognized.append({'text':content,'bbox':box,'color':'#101820'})
        if len(recognized)>=600:
            warnings.append('Text count capped at 600 items');break
    matched_refs=[x.group(1) for x in _DEEPSEEK_REF.finditer(raw)]
    unpositioned=max(0,len(matched_refs)-len(recognized))
    # Content without ref/box tokens can be prose, tables, or OCR Markdown.
    remainder=_DEEPSEEK_BOX.sub('',raw)
    if not matched_refs and remainder.strip():
        unpositioned+=1
    if bad_boxes:warnings.append(f'{bad_boxes} missing/invalid OCR coordinate boxes')
    if merged:warnings.append(f'{merged} multi-box labels merged for review')
    if unpositioned:warnings.append(f'{unpositioned} unpositioned OCR segments were not added as editable text')
    if finish=='length':warnings.append('OCR 输出达到长度上限：仅保留完整闭合且坐标有效的文字框；结果不完整，须人工检查。需要补识别时请手动授权分区 OCR。')
    reported=data.get('page_count')
    if reported is not None and reported!=len(pages):
        warnings.append('OCR page_count does not match returned pages')
    return {'texts':recognized,'shapes':[],'unpositioned':unpositioned,
            'page_count':len(pages),'model':'deepseek-ocr','warnings':warnings,
            'usage':data.get('usage') if isinstance(data.get('usage'),dict) else {},
            'finish_reason':finish or 'unknown', 'partial':finish=='length', 'needs_review':True}


def _ocr_network_message(exc: Exception) -> str:
    """Classify a requests transport failure without including URLs or secrets.

    No OCR POST is retried automatically: a disconnect after upload does not
    cancel recognition or prove that no successful-page charge occurred.
    """
    import requests
    err = requests.exceptions
    if isinstance(exc, err.ConnectTimeout):
        return ('DeepSeek OCR 建立连接超时：检查网络、代理和防火墙。'
                '请求未建立连接，但不建议自动重试。请先运行免费网络诊断。')
    if isinstance(exc, err.ReadTimeout):
        return ('DeepSeek OCR 等待响应超时：图片可能已上传且服务端仍在识别，'
                '不应立即重复提交；请先核对账单并检查网络稳定性。')
    if isinstance(exc, err.ProxyError):
        return ('DeepSeek OCR 代理连接失败：检查 HTTP(S)_PROXY 环境变量、'
                '系统代理及代理软件是否正常。请先运行免费网络诊断。')
    if isinstance(exc, err.SSLError):
        return ('DeepSeek OCR HTTPS/TLS 证书验证失败：检查系统时间、'
                '证书信任链及代理的 HTTPS 检查设置；不要关闭证书验证。')
    if isinstance(exc, err.ConnectionError):
        # Requests/urllib3 may wrap socket.gaierror with NewConnectionError.
        chain = [exc]
        seen = set()
        for item in chain:
            if id(item) in seen: continue
            seen.add(id(item))
            chain.extend(x for x in (getattr(item, '__cause__', None),
                                      getattr(item, '__context__', None)) if x is not None)
        detail = str(exc).lower()
        dns_related = any(isinstance(x, socket.gaierror) or
                          'nameresolutionerror' in type(x).__name__.lower() for x in chain)
        if dns_related or any(k in detail for k in ('name resolution','getaddrinfo failed',
                                                   'temporary failure in name resolution',
                                                   'failed to resolve')):
            return 'DeepSeek OCR DNS 解析失败：检查 api.deepseek.com 的解析、DNS、网络或代理设置。'
        return ('DeepSeek OCR 网络连接中断：可能是本地网络、代理或服务端关闭连接。'
                '若已上传图片，服务端仍可能完成识别；不要自动重试。')
    if isinstance(exc, err.RequestException):
        return ('DeepSeek OCR 请求传输失败：先使用免费网络诊断确认连接情况。'
                '如文件已上传，不排除服务端继续完成并计费。')
    return 'DeepSeek OCR 本地请求处理出错，请运行诊断工具并核对 Python 依赖。'


def diagnose_deepseek_connection(config: AIConfig, *, check_auth: bool = False) -> dict:
    """Non-billable diagnostic: DNS lookup + GET /models; uploads NO image.

    A DNS error with a working HTTP proxy can be harmless; report both tests.
    The API key is optional; without it, 401 still proves HTTPS reachability.
    """
    if urlsplit(validated_url(config.base_url)).hostname != 'api.deepseek.com':
        raise ValueError('此诊断仅用于 DeepSeek 官方 api.deepseek.com')
    import urllib.request
    report = {'endpoint':'https://api.deepseek.com/models',
              'uses_proxy':bool(urllib.request.getproxies()),
              'dns':'未测试', 'https':'未测试', 'authenticated':False}
    try:
        addrs=socket.getaddrinfo('api.deepseek.com',443,proto=socket.IPPROTO_TCP)
        report['dns']='成功' if addrs else '无解析结果'
    except OSError:
        report['dns']='失败（使用代理时可能仍能访问）'
    key = config.key() if check_auth else ''
    headers = {'Authorization':'Bearer '+key} if key else {}
    try:
        with _session() as sess:
            with sess.get(report['endpoint'], headers=headers,timeout=(8,12),
                          allow_redirects=False,stream=True) as response:
                status=response.status_code
        report['http_status']=status
        if status==200:
            report['https']='成功（HTTP 200）'
            report['authenticated']=bool(headers)
            report['advice']='API 可连接；再运行 OCR 单图模式，若仍报错重点检查上传阶段。'
        elif status==401:
            report['https']='成功（HTTP 401）'
            report['advice']=('服务器已连通，但当前 Key 未通过验证。请检查密钥或撤销状态。'
                              if headers else '服务器已连通。需要填写 Key 才能验证账户认证。')
        elif status in (402,403):
            report['https']=f'成功（HTTP {status}）'
            report['advice']='网络可达，请检查账户余额或权限。'
        elif 300<=status<400:
            report['https']=f'被重定向（HTTP {status}）'
            report['advice']='请检查系统代理或网络网关；本工具为保护 Key 不跟随跳转。'
        else:
            report['https']=f'服务器响应（HTTP {status}）'
            report['advice']='网络有响应，但接口返回非预期状态，请核对服务状态和代理。'
    except Exception as exc:
        report['https']='失败'
        report['advice']=_ocr_network_message(exc)
    return report


def deepseek_ocr_image(img:np.ndarray,config:AIConfig, *, purpose:str='text_recognition') -> dict:
    """Official POST /v1/tools/ocr. One upload = potentially billable one page.

    No automatic retries: after a transport timeout the server may finish and
    charge even if the client has disconnected. Only a user can re-authorize.
    """
    if purpose not in ('text_recognition','rich_layout_recognition'):
        raise ValueError('Unknown DeepSeek OCR purpose')
    headers=_credentials(config)
    buffer=io.BytesIO()
    Image.fromarray(img.astype(np.uint8)).convert('RGB').save(buffer,'PNG',optimize=True)
    if buffer.tell()>100*1024*1024:
        raise ValueError('DeepSeek official OCR maximum file size is 100 MiB')
    try:
        with _session() as sess:
            response=sess.post(
                _deepseek_ocr_url(config), headers=headers,
                data={'purpose':purpose},
                files={'file':('scientific-diagram.png',buffer.getvalue(),'image/png')},
                timeout=(15,max(190,config.timeout)),allow_redirects=False)
        if 300<=response.status_code<400:
            raise AIServiceError('DeepSeek OCR redirect blocked to protect your key')
        data=_read_response(response)
        if isinstance(data.get('error'), dict):
            reason=str(data['error'].get('message','')).lower()
            if 'ocr did not finish' in reason:
                raise AIServiceError('DeepSeek 官方 OCR 处理超时：官方说明失败或超时不计费。建议检查图片大小后再单独授权重新识别。')
            if 'temporarily unavailable' in reason or 'internal server error' in reason:
                raise AIServiceError('DeepSeek 官方 OCR 服务暂时不可用：请稍后再决定是否重新提交。')
            raise AIServiceError('DeepSeek OCR 返回服务端错误；请检查官方平台服务状态。')
        parsed=parse_deepseek_ocr(data)
        parsed['purpose']=purpose
        return parsed
    except AIServiceError:
        raise
    except Exception as e:
        raise AIServiceError(_ocr_network_message(e)) from e

def edit_remove_text(img: np.ndarray, mask: np.ndarray, config: AIConfig):
    """Explicit optional /images/edits request; returns an RGB array, never downloads URLs.

    The mask is advisory: alpha=0 on the requested edit region. Image models
    may ignore it or alter content elsewhere. Caller MUST perform verification.
    """
    # DeepSeek official currently documents visual analysis and OCR, but not
    # OpenAI-compatible /images/edits. Never try this path automatically.
    if urlsplit(validated_url(config.base_url)).hostname == 'api.deepseek.com':
        raise AIServiceError('DeepSeek official API does not document /images/edits; choose local inpainting instead')
    headers = _credentials(config)
    if not np.any(mask):
        raise ValueError("No OCR text mask to clean")
    if img.shape[:2] != mask.shape:
        raise ValueError("Mask dimensions must match input")
    img_buf=io.BytesIO();Image.fromarray(img).save(img_buf,"PNG")
    mask_img=np.full((*mask.shape,4),255,dtype=np.uint8)
    mask_img[mask>0,3]=0
    mask_buf=io.BytesIO();Image.fromarray(mask_img,"RGBA").save(mask_buf,"PNG")
    fields={"model":config.edit_model, "prompt":(
        "Erase only the visible text covered by the transparent mask. "
        "Reconstruct the original shapes, arrows, gridlines and colors behind those glyphs. "
        "Preserve all layout, dimensions, paths, colors, line widths, and every unmasked pixel "
        "as accurately as possible. Do not add, rearrange or redraw other items."
    )}
    try:
        with _session() as sess:
            r=sess.post(_endpoint(config,"/images/edits"),headers=headers,
                        data=fields,files={"image":("diagram.png",img_buf.getvalue(),"image/png"),
                                           "mask":("mask.png",mask_buf.getvalue(),"image/png")},
                        timeout=(10,max(config.timeout,180)),allow_redirects=False)
        if 300 <= r.status_code < 400:
            raise AIServiceError("Image-edit API redirect blocked to protect your key")
        response=_read_response(r)
        raw=response["data"][0].get("b64_json")
        if not isinstance(raw,str):
            raise AIServiceError("Image editor returned no base64 image; URL-only output is not supported")
        data=base64.b64decode(raw,validate=True)
        if len(data)>MAX_REPLY:
            raise AIServiceError("Edited image too large")
        with Image.open(io.BytesIO(data)) as p:
            if p.width*p.height>20_000_000:
                raise AIServiceError("Edited image pixel count exceeds safety limit")
            return np.asarray(p.convert("RGB")),response.get("usage",{})
    except AIServiceError:
        raise
    except (KeyError,IndexError,ValueError,TypeError) as e:
        raise AIServiceError("Image-edit API response format is invalid") from e
    except Exception as e:
        raise AIServiceError("AI image editing request failed (network/timeout/TLS).") from e


def check_clean_image(original, cleaned, mask, *, max_unmasked_mae=14.0):
    """Validate geometric stability against the unmasked source, not hallucinated accuracy."""
    import cv2
    if cleaned.shape != original.shape:
        cleaned = cv2.resize(cleaned, (original.shape[1],original.shape[0]), interpolation=cv2.INTER_LANCZOS4)
    outside=cv2.dilate(mask,np.ones((13,13),dtype=np.uint8),iterations=1)==0
    if not np.any(outside):
        return cleaned, float('inf'), False
    diff=np.abs(cleaned.astype(np.float32)-original.astype(np.float32)).mean(axis=2)
    mae=float(np.mean(diff[outside]))
    return cleaned,round(mae,3),mae <= max_unmasked_mae
