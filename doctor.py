#!/usr/bin/env python3
"""Check local dependency availability without transmitting files or secrets."""
import importlib.util
import json
import shutil
import sys
from ai_bridge import AIConfig,load_profile

def diagnose():
    modules={'numpy':'numpy','opencv':'cv2','Pillow':'PIL','python-pptx':'pptx',
             'defusedxml':'defusedxml','shapely':'shapely','Tesseract wrapper':'pytesseract',
             'VTracer':'vtracer','CairoSVG':'cairosvg','HTTP client':'requests',
             'OS keyring':'keyring'}
    available={name:bool(importlib.util.find_spec(mod)) for name,mod in modules.items()}
    try:profile=load_profile()
    except Exception:profile={}
    return {'python':sys.version.split()[0],
            'modules':available,'tesseract_executable':shutil.which('tesseract') is not None,
            'api_profile_exists':bool(profile),
            'api_key_set_in_environment':bool(__import__('os').environ.get(profile.get('key_env',AIConfig.key_env))),
            'network_requests_performed':0}

if __name__=='__main__':
    print(json.dumps(diagnose(),indent=2,ensure_ascii=False))
