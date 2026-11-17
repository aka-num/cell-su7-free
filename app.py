"""Cell SU7 Remastered 3.5: local-first reconstruction with opt-in AI API."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import threading

from ai_bridge import AIConfig, load_profile, save_profile, store_key, forget_key, probe_provider
from engine import Settings, convert


def make_config(values: dict, key: str = ''):
    profile = load_profile()
    return AIConfig(**{
        'base_url': values.get('base_url') or profile.get('base_url') or AIConfig.base_url,
        'vision_model': values.get('vision_model') or profile.get('vision_model') or AIConfig.vision_model,
        'edit_model': values.get('edit_model') or profile.get('edit_model') or AIConfig.edit_model,
        'key_env': values.get('key_env') or profile.get('key_env') or AIConfig.key_env,
        'timeout': values.get('timeout') or profile.get('timeout') or AIConfig.timeout,
        'api_key': key,
    }).validate()


def cli():
    p = argparse.ArgumentParser(prog='cell-su7', description='Local-first image to editable PPTX/SVG/PDF; opt-in external AI')
    p.add_argument('source', nargs='?', help='Image/SVG input; omit to launch the GUI')
    p.add_argument('-o', '--output', help='Editable .pptx, .svg or .pdf')
    p.add_argument('--manifest', help='Optional curated text manifest JSON')
    p.add_argument('--visual-edits', help='Reuse approved canvas object offsets from visual-edits.json (offline)')
    p.add_argument('--manifest-out', help='Export recognized text manifest')
    p.add_argument('--svg-out', help='Also export editable SVG')
    p.add_argument('--pdf-out', help='Also export vector PDF (CairoSVG)')
    p.add_argument('--colors', type=int, default=32)
    p.add_argument('--min-area', type=int, default=18)
    p.add_argument('--simplify', type=float, default=.0015)
    p.add_argument('--max-shapes', type=int, default=4000)
    p.add_argument('--max-pixels', type=int, default=3000000)
    p.add_argument('--width', type=float, default=12.0)
    p.add_argument('--ocr', action='store_true', help='FREE offline Tesseract OCR')
    p.add_argument('--ocr-lang', default='eng')
    p.add_argument('--ocr-confidence', type=int, default=40)
    p.add_argument('--no-inpaint', action='store_true')
    p.add_argument('--backend', choices=['auto', 'precision', 'contour', 'vtracer'], default='auto')
    p.add_argument('--precision-tile', type=int, default=192, help='High-fidelity tracing tile size (32+ pixels)')
    p.add_argument('--precision-separate-regions', action='store_true', help='Split large connected regions for better editing')
    p.add_argument('--precision-min-region-area', type=int, default=64, help='Only separate components >= this many pixels')
    p.add_argument('--precision-max-commands', type=int, default=2000000, help='Complexity safety limit')
    p.add_argument('--color-mode',choices=['legacy','balanced','perceptual','fusion'],default='perceptual',help='Balanced preserves edge topology; perceptual minimizes LAB color error')
    p.add_argument('--luminance-weight',type=float,default=1.7,help='LAB luma weighting (perceptual mode)')
    p.add_argument('--background-gradient',action=argparse.BooleanOptionalAction,default=True,help='Editable gradient background when confidently detected')
    p.add_argument('--structure-mode',choices=['off','annotate','hybrid','objects'],default='objects',help='Heuristic connected-region geometry, annotated graph')
    p.add_argument('--scene-graph-out',help='Export structure candidates and repetition groups JSON')
    p.add_argument('--ocr-layout', choices=['line', 'word'], default='line')
    p.add_argument('--group-regions', action='store_true')
    p.add_argument('--report', help='Offline quality HTML report')
    # All AI options are opt-in. The key is taken from an environment variable or the OS keyring.
    # Deliberately no --api-key: command-line arguments may appear in shell history/process listings.
    p.add_argument('--ai-mode', choices=['off', 'ocr', 'hybrid', 'layout', 'deepseek-ocr'], default='off',
                   help='off: no network; ocr: AI text boxes; layout: AI editable semantic diagram')
    p.add_argument('--ai-base-url', help='Trusted OpenAI-compatible API base URL, e.g. https://api.openai.com/v1')
    p.add_argument('--ai-model', help='Multimodal chat model supporting image_url')
    p.add_argument('--ai-edit-model', help='Image-edit model for /images/edits')
    p.add_argument('--api-key-env', help='Name of environment variable containing the API key')
    p.add_argument('--ai-clean', action='store_true', help='Upload source and local text mask to /images/edits')
    p.add_argument('--ai-accept-changes', action='store_true', help='Allow edited image to change non-text regions')
    p.add_argument('--ai-timeout', type=float, help='Timeout for remote AI calls (seconds)')
    p.add_argument('--deepseek-tiling',choices=['single','2x2'],default='single',
                   help='DeepSeek OCR: 1 request or 4 tiled requests (extra cost)')
    p.add_argument('--deepseek-purpose',choices=['text_recognition','rich_layout_recognition'],default='text_recognition')
    p.add_argument('--manifest-clean',action='store_true',
                   help='Explicitly remove text via approved bbox values in --manifest, offline')
    p.add_argument('--ai-test', action='store_true', help='Explicitly test /models endpoint; no source needed')
    p.add_argument('--ai-save-profile', action='store_true', help='Save URL/model settings only, NEVER the API key')
    p.add_argument('--clean-preview', help='Save text-cleaned intermediate PNG for verification')
    args = p.parse_args()
    try:
        if args.ai_mode=='deepseek-ocr' and not args.ai_base_url:
            # Avoid accidentally sending DeepSeek OCR uploads to the profile's
            # unrelated OpenAI-compatible endpoint.
            args.ai_base_url='https://api.deepseek.com'
            if not args.ai_model:args.ai_model='deepseek-flash'
        config = make_config({'base_url':args.ai_base_url, 'vision_model':args.ai_model,
                              'edit_model':args.ai_edit_model, 'key_env':args.api_key_env,
                              'timeout':args.ai_timeout})
        if args.ai_save_profile:
            print('Saved non-secret AI profile:', save_profile(config))
        if args.ai_test:
            print(probe_provider(config))
            return 0
        if not args.source:
            if args.output:
                p.error('Source required when an output path is supplied')
            if args.ai_save_profile:
                return 0
            gui(); return 0
        if not args.output:
            p.error('Specify -o output.pptx (or .svg / .pdf)')
        setting=Settings(colors=args.colors,min_area=args.min_area,simplify=args.simplify,
                         max_shapes=args.max_shapes,max_pixels=args.max_pixels,slide_width=args.width,
                         ocr=args.ocr,ocr_lang=args.ocr_lang,ocr_confidence=args.ocr_confidence,
                         inpaint=not args.no_inpaint,backend=args.backend,ocr_layout=args.ocr_layout,
                         group_regions=args.group_regions,precision_tile=args.precision_tile,
                         precision_max_commands=args.precision_max_commands,
                         precision_separate_regions=args.precision_separate_regions,
                         precision_min_region_area=args.precision_min_region_area,
                         color_mode=args.color_mode,luminance_weight=args.luminance_weight,
                         background_gradient=args.background_gradient,
                         structure_mode=args.structure_mode,ai_mode=args.ai_mode,ai_config=config,
                         ai_remote_clean=args.ai_clean,ai_accept_changes=args.ai_accept_changes,
                         clean_preview=args.clean_preview,
                         deepseek_tiling=args.deepseek_tiling,deepseek_purpose=args.deepseek_purpose,
                         manifest_clean=args.manifest_clean)
        result = convert(args.source,args.output,setting,args.manifest,args.svg_out,
                         args.pdf_out,args.manifest_out,args.report,edit_plan=args.visual_edits,
                         scene_graph_out=args.scene_graph_out)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as e:
        print(f'ERROR: {e}',file=sys.stderr)
        return 1


def gui():
    """Run the guided studio; CLI operation is unchanged."""
    try:
        from studio_ui import launch
    except ImportError as e:
        raise SystemExit('Tkinter GUI unavailable; use the CLI instead') from e
    return launch()


if __name__=='__main__':
    raise SystemExit(cli())
