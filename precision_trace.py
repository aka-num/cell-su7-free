"""Pixel-boundary vector tracing: true editable paths, zero raster embedding.

The earlier contour backend found contour *pixel centers*, discarding 1px marks
and shrinking fine details. This backend traces the boundaries of quantized
pixel *cells*. It retains holes and islands, using a deterministic four-way
junction policy. It trades the size of its output for fidelity; this is not a
semantic object detector or a neural vectorizer.

Author: Cell SU7 Remastered contributors. MIT.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import numpy as np
import cv2
from PIL import Image
from perceptual_color import build_color_plan
from structure_aware import fit_primitive, repeated_groups, detect_band_hints

# Image coordinate system: x right, y down; clockwise = right turn.
_DX = (1, 0, -1, 0)
_DY = (0, 1, 0, -1)


@dataclass(frozen=True)
class TraceOptions:
    colors: int = 24
    tile_size: int = 192
    max_shapes: int = 12000
    max_commands: int = 2_000_000
    simplify: float = 0.0
    # Preserve every quantized-pixel island by default, even single pixels.
    min_pixels: int = 1
    separate_regions: bool = False
    min_region_area: int = 64
    color_mode: str = "legacy"  # legacy / balanced / perceptual
    luminance_weight: float = 1.0
    gradient_background: bool = False
    structure_mode: str = "off"  # off / annotate / hybrid / objects


def _tile_boundaries(mask: np.ndarray, offset_x: int, offset_y: int):
    """Return clockwise grid-edge loops (at pixel cell corners) for one mask.

    Orthogonal edges are exact; diagonal-touching pixels remain separate loops.
    The four-way turning rule resolves a shared vertex without ambiguity.
    """
    if mask.ndim != 2 or mask.dtype != np.bool_:
        mask = np.asarray(mask, dtype=bool)
    h, w = mask.shape
    if not np.any(mask):
        return []
    edges: dict[tuple[int, int], int] = {}
    # bitmask encodes which of east/south/west/north is outgoing at a vertex.
    def add(xs, ys, direction):
        bit = 1 << direction
        for x, y in zip(xs.tolist(), ys.tolist()):
            key = (x + offset_x, y + offset_y)
            edges[key] = edges.get(key, 0) | bit

    top = mask.copy();top[1:] &= ~mask[:-1]
    y, x = np.nonzero(top);add(x, y, 0)
    right = mask.copy();right[:, :-1] &= ~mask[:, 1:]
    y, x = np.nonzero(right);add(x + 1, y, 1)
    bottom = mask.copy();bottom[:-1] &= ~mask[1:]
    y, x = np.nonzero(bottom);add(x + 1, y + 1, 2)
    left = mask.copy();left[:, 1:] &= ~mask[:, :-1]
    y, x = np.nonzero(left);add(x, y + 1, 3)

    loops = []
    while edges:
        start = next(iter(edges))
        start_bits = edges[start]
        direction = next(d for d in range(4) if start_bits & (1 << d))
        pos = start
        loop = [start]
        prev_direction = None
        guard = 0
        while True:
            bits = edges[pos]
            if not bits & (1 << direction):
                raise ValueError('Broken vector boundary: missing directed edge')
            rem = bits & ~(1 << direction)
            if rem: edges[pos] = rem
            else: del edges[pos]
            next_pos = (pos[0] + _DX[direction], pos[1] + _DY[direction])
            if direction == prev_direction:
                loop[-1] = next_pos  # replace collinear intermediate vertex
            else:
                loop.append(next_pos)
            prev_direction = direction
            pos = next_pos
            if pos == start:
                break
            if pos not in edges:
                raise ValueError('Broken vector boundary: dangling edge')
            bits = edges[pos]
            # Stay on the same pixel at corner touching points (prefer right).
            for cand in ((direction + 1) % 4, direction, (direction + 3) % 4, (direction + 2) % 4):
                if bits & (1 << cand):
                    direction = cand
                    break
            guard += 1
            if guard > 4 * w * h + 4:
                raise ValueError('Vector boundary failed to close')
        # Discard duplicated final coordinate; Z will close exactly.
        if len(loop) >= 4:
            if loop[-1] == loop[0]:loop.pop()
            # A rectangle always yields 4 vertices, even 1x1 pixel.
            if len(loop) >= 4:
                loops.append(loop)
    return loops


def _quantize(img: np.ndarray, colors: int):
    """Palette+label image with NO dither, preserve original image resolution."""
    rgb = Image.fromarray(img, mode='RGB')
    pal = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    ids = np.asarray(pal, dtype=np.uint8)
    palette = np.asarray(pal.getpalette(), dtype=np.uint8).reshape(-1, 3)
    return ids, palette


def trace_scene(img: np.ndarray, options: TraceOptions):
    """Convert palette cells to ordered native geometric Shape objects.

    Imports engine only here to avoid a circular import at module import time.
    Several disjoint islands of a color within one tile share an object.
    This creates tile-addressable editable objects, not per-pixel PPT shapes.
    """
    from engine import Shape, Scene, VectorPath, polygon
    from geometry import ellipse
    if img.ndim != 3 or img.shape[-1] != 3:
        raise ValueError('Expected uint8 RGB image')
    if options.colors < 2 or options.colors > 128:
        raise ValueError('precision tracing supports 2..128 palette colors')
    if options.tile_size < 32:
        raise ValueError('tile size must be at least 32 pixels')
    if options.structure_mode not in ('off', 'annotate', 'hybrid', 'objects'):
        raise ValueError('structure_mode must be off, annotate, hybrid or objects')
    if options.color_mode not in ('legacy', 'balanced', 'perceptual', 'fusion'):
        raise ValueError('color_mode must be legacy, balanced, perceptual or fusion')
    if options.gradient_background or options.color_mode != 'legacy':
        plan = build_color_plan(img, options.colors, options.color_mode, options.gradient_background, options.luminance_weight)
        ids, palette = plan.ids, plan.palette
        background_idx = -1 if plan.gradient else -2
        background_color = plan.background_rgb
        color_diagnostics = plan.diagnostics
        grad = plan.gradient
        if not grad:
            # Flat backgrounds still need the most common border palette cell
            border = np.concatenate((ids[0], ids[-1], ids[:, 0], ids[:, -1]))
            background_idx = int(np.bincount(border[border>=0], minlength=max(len(palette), 1)).argmax())
            background_color = tuple(int(v) for v in palette[background_idx])
    else:
        ids, palette = _quantize(img, options.colors)
        border = np.concatenate((ids[0], ids[-1], ids[:, 0], ids[:, -1]))
        background_idx = int(np.bincount(border, minlength=256).argmax())
        background_color = tuple(int(x) for x in palette[background_idx])
        color_diagnostics = {'palette_mode':'legacy','background_gradient':False}
        grad = None
    h, w = ids.shape
    scene = Scene(float(w), float(h), [], [])
    scene.add_shape(Shape(polygon([(0, 0), (w, 0), (w, h), (0, h)]), background_color,
                          name='precision_background', gradient=grad))
    graph = []
    def record(shape_index, name, kind, bbox, color, pixel_area):
        graph.append({'shape_index':int(shape_index), 'name':name, 'kind':kind,
                      'bbox':[int(v) for v in bbox], 'rgb':list(map(int,color)),
                      'area_pixels':int(pixel_area)})
    record(0, 'precision_background', 'gradient_background' if grad else 'solid_background',
           (0, 0, w, h), background_color, w*h)

    seen_commands = 5
    # Drawing the complementary palette cells without overlap is equivalent to
    # a complete exact-cell segmentation of the palette-quantized image.
    # Tile-local shapes keep the objects small enough for downstream editors.
    ts = options.tile_size
    def add_mask(mask: np.ndarray, x: int, y: int, rgb: tuple[int, int, int], name: str,
                 kind: str='color_cluster', use_ellipse: bool=False,
                 primitive_proposal=None):
        nonlocal seen_commands
        loops = _tile_boundaries(mask, x, y)
        if not loops:
            return
        cmds = []
        fitted = None
        if use_ellipse and len(loops) == 1 and mask.sum() >= 80:
            # Geometric promotion is gated by pixel-mask IoU. A complex protein
            # silhouette must never be replaced with an invented oval.
            raw = np.where(mask, 255, 0).astype(np.uint8)
            contours, _ = cv2.findContours(raw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if len(contours) == 1 and len(contours[0]) >= 5:
                (cx,cy),(ax1,ax2),angle = cv2.fitEllipse(contours[0])
                if min(ax1,ax2) >= 3:
                    check = np.zeros(raw.shape, np.uint8)
                    cv2.ellipse(check,((float(cx),float(cy)),(float(ax1),float(ax2)),float(angle)),255,-1)
                    overlap = np.count_nonzero((check > 0)&mask)
                    union = np.count_nonzero((check > 0)|mask)
                    if union and overlap / union >= .985 and abs(angle) < 3:
                        # Convert contour pixel centers to pixel-cell outline.
                        fitted = ellipse(x+cx+.5,y+cy+.5,ax1/2+.35,ax2/2+.35)
                        kind = 'native_ellipse'
        if primitive_proposal is not None:
            fitted = primitive_proposal.path
            kind = primitive_proposal.kind
        if fitted is not None:
            cmds = fitted.commands
        else:
            for pts in loops:
                cmds.append(('M', (float(pts[0][0]), float(pts[0][1]))))
                cmds.extend(('L', (float(px), float(py))) for px, py in pts[1:])
                cmds.append(('Z', ()))
        seen_commands += len(cmds)
        if seen_commands > options.max_commands:
            raise ValueError('Precision path complexity limit reached; reduce palette colors or increase --precision-max-commands')
        new_shape = Shape(VectorPath(cmds), rgb, name=name)
        if primitive_proposal is not None:
            new_shape.primitive = primitive_proposal.primitive
            if not primitive_proposal.fill:
                new_shape.fill = None
                new_shape.stroke = rgb
                new_shape.stroke_width = primitive_proposal.stroke_width
        elif kind == 'native_ellipse':
            new_shape.primitive = 'ellipse'
        scene.add_shape(new_shape)
        ys,xs = np.nonzero(mask)
        if len(xs):record(len(scene.shapes)-1, name, kind,
                           (x+int(xs.min()),y+int(ys.min()),x+int(xs.max())+1,y+int(ys.max())+1),rgb,len(xs))
        if len(scene.shapes) > options.max_shapes:
            raise ValueError('Precision shape limit reached; increase --max-shapes or disable separate regions')

    region_count = 0
    native_count = 0
    # New opt-in object mode: recognize connected color islands across ALL
    # tile seams. Promote to primitives only under strict mask-IoU checks.
    # All unpromoted pixels continue through the exact-cell tracer untouched.
    if options.structure_mode == 'objects':
        ids = ids.astype(np.int16, copy=True)
        for value in sorted(int(a) for a in np.unique(ids) if int(a) >= 0):
            if value == background_idx:continue
            mask=(ids==value).astype(np.uint8)
            n,labels,stats,_=cv2.connectedComponentsWithStats(mask,connectivity=4)
            # Skip pathological images with enormous component counts; this is
            # a fidelity and memory guard, not a silent raster fallback.
            for ri in range(1,n):
                x0,y0,bw,bh,area=(int(v) for v in stats[ri])
                if not (16 <= area <= 14000 and bw <= 200 and bh <= 200):continue
                crop=labels[y0:y0+bh,x0:x0+bw]==ri
                proposed=fit_primitive(crop,x0,y0,strict=.985)
                if proposed is None:continue
                col=tuple(int(v) for v in palette[value])
                add_mask(crop,x0,y0,col, f'object_{value}_{ri}',
                         kind=proposed.kind, primitive_proposal=proposed)
                # Mask only this connected component, never any neighboring
                # similarly colored regions or background pixels.
                ids[y0:y0+bh,x0:x0+bw][crop]=-3
                native_count+=1
    for y0 in range(0, h, ts):
        for x0 in range(0, w, ts):
            section = ids[y0:min(h, y0 + ts), x0:min(w, x0 + ts)]
            values, counts = np.unique(section, return_counts=True)
            for value, count in zip(values, counts):
                value = int(value)
                if value < 0 or value == background_idx or count < options.min_pixels:
                    continue
                mask = section == value
                col = tuple(int(n) for n in palette[value])
                if options.separate_regions or options.structure_mode == 'hybrid':
                    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=4)
                    small = mask.copy()
                    for region_id in range(1, n):
                        rx, ry, rw, rh, area = map(int, stats[region_id])
                        if area < options.min_region_area:
                            continue
                        sub = labels[ry:ry+rh, rx:rx+rw] == region_id
                        ratio = max(rw/rh, rh/rw)
                        kind = ('connector_candidate' if ratio >= 6 and area < rw*rh*.4
                                else 'compact_region' if .4 <= rw/rh <= 2.5 else 'freeform_region')
                        add_mask(sub, x0+rx, y0+ry, col,
                                 f'precision_region_{x0}_{y0}_{value}_{region_id}',
                                 kind=kind, use_ellipse=(options.structure_mode=='hybrid'))
                        small[ry:ry+rh, rx:rx+rw][sub] = False
                        region_count += 1
                    if np.any(small):
                        add_mask(small, x0, y0, col, f'precision_details_{x0}_{y0}_{value}',
                                 kind='fine_details')
                else:
                    add_mask(mask, x0, y0, col, f'precision_t{x0}_{y0}_c{value}')
    # Stable high-level scene graph: repeated objects are descriptive groups,
    # not false claims that distinct cells/proteins were biologically identified.
    buckets = {}
    for item in graph[1:]:
        if item['kind'] not in ('compact_region','native_ellipse'):
            continue
        a,b,c,d = item['bbox']; width=c-a; height=d-b
        if width < 4 or height < 4:continue
        key=(tuple(item['rgb']), round(width/5), round(height/5))
        buckets.setdefault(key,[]).append(item['shape_index'])
    groups=[{'group_id':f'repetition_{i+1}', 'kind':'similar_color_and_size', 'member_shape_indices':items}
            for i, items in enumerate(v for v in buckets.values() if len(v)>=3)]
    if options.structure_mode == 'objects':
        groups=repeated_groups(graph[1:])
    hints=detect_band_hints(img) if options.structure_mode in ('hybrid','objects') else []
    scene.structure_graph = {'schema_version':2,'canvas':[w,h],
                             'objects':graph,'repetition_groups':groups,
                             'region_hints':hints,
                             'native_primitive_count':native_count,
                             'disclaimer':'Visual geometry proposals; no biological semantic inference.'}
    scene.warnings.append('Perceptual tracing preserves quantized pixel geometry; region classes and repetition groups are heuristic candidates, not biological labels.')
    return scene, {'precision_colors': options.colors, 'precision_tile': ts,
                   'precision_path_commands': seen_commands, 'precision_shape_count': len(scene.shapes),
                   'precision_background': list(background_color), 'separate_regions': options.separate_regions,
                   'structure_mode':options.structure_mode, 'repetition_groups':len(groups),
                   'separate_region_count': region_count,
                   'native_primitive_count':native_count, 'band_hints':len(hints), **color_diagnostics}
