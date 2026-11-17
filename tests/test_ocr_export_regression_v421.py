"""Regression tests for OCR line export and the tempfile scope failure.

No external AI connection is required: all conversions run locally.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib.util
import pytest
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation

from engine import Settings, convert


def _sample(tmp_path):
    image = Image.new('RGB', (420, 130), '#ffffff')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('DejaVuSans.ttf', 34)
    draw.text((28, 30), 'HELLO WORLD', fill='black', font=font)
    path = tmp_path / 'words.png'
    image.save(path)
    return path


def test_ocr_line_pptx_and_quality_report(tmp_path):
    pytest.importorskip('pytesseract')
    pytest.importorskip('cairosvg')
    src = _sample(tmp_path)
    ppt = tmp_path / 'editable.pptx'
    report = tmp_path / 'comparison.html'
    manifest = tmp_path / 'texts.json'
    result = convert(src, ppt, Settings(ocr=True, ocr_lang='eng', ocr_layout='line', colors=6),
                     manifest_output=manifest, report_path=report)
    assert ppt.is_file() and report.is_file() and manifest.is_file()
    assert result['texts'] >= 1
    texts = [shape.text for shape in Presentation(ppt).slides[0].shapes
             if shape.has_text_frame and shape.text.strip()]
    assert any('HELLO' in t and 'WORLD' in t for t in texts), texts
    assert len(texts) == result['texts']


def test_pptx_export_with_aux_pdf_and_report(tmp_path):
    pytest.importorskip('cairosvg')
    src = _sample(tmp_path)
    ppt, pdf, report = tmp_path / 'out.pptx', tmp_path / 'out.pdf', tmp_path / 'compare.html'
    result = convert(src, ppt, Settings(colors=5), extra_pdf=pdf, report_path=report)
    assert ppt.is_file() and pdf.is_file() and pdf.stat().st_size > 100
    assert report.is_file() and result.get('quality', {}).get('ssim') is not None


def test_pdf_primary_export_and_svg(tmp_path):
    pytest.importorskip('cairosvg')
    src = _sample(tmp_path)
    pdf, svg = tmp_path / 'out.pdf', tmp_path / 'out.svg'
    result = convert(src, pdf, Settings(colors=5), extra_svg=svg)
    assert pdf.is_file() and svg.is_file() and result['output'] == str(pdf)
