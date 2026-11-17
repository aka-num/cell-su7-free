#!/usr/bin/env python3
"""Safe free connectivity check for DeepSeek (no OCR/image submission)."""
from __future__ import annotations
import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_bridge import AIConfig, diagnose_deepseek_connection


def main(argv=None):
    parser=argparse.ArgumentParser(description='Free DeepSeek DNS/TLS/auth diagnostic; does not upload images')
    parser.add_argument('--check-key', action='store_true', help='Also test authentication using a hidden API key')
    args=parser.parse_args(argv)
    key=''
    if args.check_key:
        key=os.environ.get('DEEPSEEK_API_KEY','').strip() or getpass('DeepSeek API Key (hidden): ').strip()
        if not key:
            parser.error('Missing API Key')
    config=AIConfig(base_url='https://api.deepseek.com',vision_model='deepseek-flash',
                    api_key=key,key_env='DEEPSEEK_API_KEY' if args.check_key else 'CELL_SU7_NO_AUTH_CHECK_KEY')
    report=diagnose_deepseek_connection(config,check_auth=args.check_key)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print('诊断仅访问 /models；没有发送科研图、没有调用 OCR，也不会消耗 OCR 页数。')
    return 0 if report['https'].startswith('成功') else 1

if __name__=='__main__':
    raise SystemExit(main())
