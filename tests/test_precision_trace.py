"""Regression tests for v4 pixel-edge tracing, preservation and native exports."""
from __future__ import annotations
import io
import sys
from pathlib import Path
import numpy as np
import pytest
from PIL import Image, ImageDraw
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from precision_trace import _tile_boundaries
from engine import Settings, create_scene, write_svg, write_pptx


def test_pixel_singleton_boundary_area():
    assert _tile_boundaries(np.array([[True]], bool), 9, 14) == [
        [(9, 14), (10, 14), (10, 15), (9, 15)]]


def test_diagonal_pixels_not_merged():
    pts = _tile_boundaries(np.array([[True, False], [False, True]], bool), 0, 0)
    assert len(pts) == 2
    assert all(len(x) == 4 for x in pts)


def test_compound_hole_opposite_winding():
    mask = np.ones((5, 5), bool)
    mask[1:4, 1:4] = False
    contours = _tile_boundaries(mask, 0, 0)
    assert len(contours) == 2
    def signed(poly):
        return sum(x*y2-x2*y for (x,y),(x2,y2) in zip(poly, poly[1:]+poly[:1]))
    assert signed(contours[0]) * signed(contours[1]) < 0


def test_native_svg_preserves_small_pixels(tmp_path):
    cairosvg = pytest.importorskip('cairosvg')
    image = Image.new('RGB', (37, 29), '#fffefe')
    d = ImageDraw.Draw(image)
    d.rectangle((3,3,19,17), fill='#ef1234')
    d.rectangle((9,7,14,13), fill='#fffefe')
    d.point((23,25), fill='#1122dd')
    d.point((35,1), fill='#1122dd')
    src = tmp_path/'source.png'; image.save(src)
    scene, stats = create_scene(src, Settings(backend='precision', colors=3, min_area=10))
    assert stats['backend'] == 'precision'
    assert len(scene.shapes) >= 2
    svg = tmp_path/'source.svg';write_svg(scene, svg)
    result = Image.open(io.BytesIO(cairosvg.svg2png(url=str(svg)))).convert('RGB')
    a = np.array(image,dtype=np.int16)
    b = np.array(result,dtype=np.int16)
    assert np.mean(np.abs(a-b)) < 2.5
    assert b[25,23,2] > 150  # 1px blue region was not filtered
    assert b[10,10,0] > 240  # interior hole


def test_precision_ppt_is_native_shapes(tmp_path):
    from pptx import Presentation
    from zipfile import ZipFile
    source = tmp_path/'src.png'
    pic = Image.new('RGB',(42,25),'white')
    ImageDraw.Draw(pic).ellipse((4,3,22,21),fill='green')
    pic.save(source)
    scene, _ = create_scene(source,Settings(backend='precision',colors=3))
    ppt = tmp_path/'out.pptx';write_pptx(scene,ppt)
    assert len(Presentation(ppt).slides[0].shapes) == len(scene.shapes)
    with ZipFile(ppt) as z:
        assert not any(p.startswith('ppt/media/') for p in z.namelist())


def test_region_separation_preserves_visual_and_increases_editability(tmp_path):
    im = Image.new('RGB',(90,50),'white')
    d=ImageDraw.Draw(im)
    for k in range(5):
        d.ellipse((5+k*16,10,12+k*16,17),fill='red')
    source=tmp_path/'test.png';im.save(source)
    basic,_=create_scene(source,Settings(backend='precision',colors=2,precision_tile=90))
    apart,_=create_scene(source,Settings(backend='precision',colors=2,precision_tile=90,
                         precision_separate_regions=True,precision_min_region_area=10))
    assert len(apart.shapes)>len(basic.shapes)
    assert any('precision_region_' in s.name for s in apart.shapes)


def test_precision_path_complexity_limit_is_explicit(tmp_path):
    im = Image.new('RGB', (32, 32), 'white')
    d=ImageDraw.Draw(im)
    for n in range(0,32,2):d.line((n,0,n,31),fill='black')
    src=tmp_path/'stripes.png';im.save(src)
    with pytest.raises(ValueError,match='complexity limit'):
        create_scene(src,Settings(backend='precision',colors=2,precision_max_commands=20))
