"""Offline color reconstruction for editable vectors.

Perceptual Lab k-means is used for the *foreground* rather than spending palette
entries on background pixels. Bright low-chroma smooth regions are represented
by an editable multi-stop vertical gradient, with conservative per-pixel gating.
No neural model / internet / paid endpoint is needed.
"""
from __future__ import annotations
from dataclasses import dataclass
import cv2
import numpy as np


@dataclass
class ColorPlan:
    ids: np.ndarray                  # -1 = paint with gradient background
    palette: np.ndarray              # K x 3, RGB uint8
    gradient: dict | None
    background_rgb: tuple[int, int, int]
    diagnostics: dict


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    values = np.asarray(rgb, dtype=np.uint8)
    if values.ndim == 2:
        values = values.reshape(1, -1, 3)
    elif values.ndim == 1:
        values = values.reshape(1, 1, 3)
    lab = cv2.cvtColor(values, cv2.COLOR_RGB2LAB).astype(np.float32)
    # OpenCV stores L 0..255, a/b offset 128; convert to CIE Lab units.
    lab[..., 0] *= (100. / 255.)
    lab[..., 1:] -= 128.
    return lab.reshape(-1, 3)


def estimate_background(img: np.ndarray, enabled: bool = True, *, threshold: float = 11.5):
    """Return (background RGB by row, bg mask, gradient descriptor or None).

    Estimation is deliberately conservative: fit medians of near-white neutral
    pixels at fixed Y bands. If there is insufficient background, use a flat
    border mode and DO NOT mask foreground pixels based on a speculative fit.
    """
    h, w, _ = img.shape
    f = img.astype(np.int16)
    spread = f.max(axis=2) - f.min(axis=2)
    bright = (f.min(axis=2) > 192) & (spread < 54)
    n = max(3, min(13, h // 30 + 1))
    ys = np.linspace(0, h - 1, n)
    medians = []
    fractions = []
    for y in ys:
        lo = max(0, int(round(y)) - max(2, h // (3 * n)))
        hi = min(h, int(round(y)) + max(2, h // (3 * n)) + 1)
        band = img[lo:hi]
        selected = band[bright[lo:hi]]
        if len(selected) < max(32, int((hi - lo) * w * 0.06)):
            continue
        medians.append(np.median(selected.astype(np.float32), axis=0))
        fractions.append(float(y / max(h - 1, 1)))
    border = np.concatenate((img[0, :, :], img[-1, :, :], img[:, 0, :], img[:, -1, :]))
    is_bright = (border.min(axis=1) > 185) & ((border.max(axis=1).astype(int)-border.min(axis=1).astype(int)) < 60)
    border_ref = np.median(border[is_bright], axis=0) if np.any(is_bright) else np.median(border, axis=0)
    flat = np.repeat(np.asarray(border_ref, dtype=np.float32)[None, :], h, axis=0)
    if not enabled or len(medians) < 3:
        return flat, np.zeros((h,w), dtype=bool), None
    med = np.asarray(medians, dtype=np.float32)
    # A uniform background should stay a solid shape, with no speculative masks.
    movement = float(np.linalg.norm(med.max(axis=0) - med.min(axis=0)))
    if movement < 10:
        return flat, np.zeros((h,w), dtype=bool), None
    fractions = np.asarray(fractions, dtype=np.float32)
    # Interpolating each measured band keeps gradients with a transition near a
    # membrane; smoothing with 3-band median prevents isolated high-light objects
    # from changing the background color abruptly.
    smooth = med.copy()
    if len(med) >= 5:
        for i in range(1, len(med)-1):
            smooth[i] = np.median(med[i-1:i+2], axis=0)
    colors = np.vstack([np.interp(np.linspace(0,1,h), fractions, smooth[:, c]) for c in range(3)]).T
    colors = np.clip(colors, 0, 255)
    # Threshold is an RGB L2 distance: only unambiguous background pixels are
    # replaced by the gradient. Anti-alias pixels stay in the foreground palette.
    delta = np.linalg.norm(img.astype(np.float32) - colors[:, None, :], axis=2)
    mask = bright & (delta <= threshold)
    # Require a meaningful percentage of fitted pixels. Otherwise avoid a
    # gradient that could wipe out a largely white foreground diagram.
    if mask.mean() < 0.12:
        return flat, np.zeros((h,w), dtype=bool), None
    stops = [{'position': round(float(p), 5), 'color': tuple(int(v) for v in np.clip(np.rint(c), 0, 255)), 'alpha':1.0}
             for p,c in zip(fractions, smooth)]
    if stops[0]['position'] != 0:
        stops.insert(0, {'position':0.,'color':stops[0]['color'],'alpha':1.0})
    if stops[-1]['position'] != 1:
        stops.append({'position':1.,'color':stops[-1]['color'],'alpha':1.0})
    gradient = {'id': 'background_gradient_local', 'type':'linear',
                'units':'objectBoundingBox','x1':'0%','y1':'0%','x2':'0%','y2':'100%',
                'stops':stops}
    return colors, mask, gradient


def perceptual_palette(img: np.ndarray, colors: int, background: np.ndarray, *, sample_limit: int=45000, luminance_weight: float=1.0):
    """Deterministic LAB palette quantization of non-background pixels."""
    if not 2 <= colors <= 128:
        raise ValueError('Invalid palette size')
    h,w = background.shape
    foreground = (~background).ravel()
    selected = np.flatnonzero(foreground)
    if selected.size < colors:
        # No interesting foreground. The entire image is a background gradient.
        return np.full((h,w),-1,dtype=np.int16), np.zeros((0,3),dtype=np.uint8)
    lab = _rgb_to_lab(img).reshape(-1,3)
    if not 0.5 <= luminance_weight <= 4:
        raise ValueError('luminance_weight must be between 0.5 and 4')
    lab[:,0] *= luminance_weight
    rgb = img.reshape(-1,3)
    rng = np.random.default_rng(23)
    # Sampling includes both broad regions and edges / saturation extremes.
    base_count = min(selected.size, sample_limit // 2)
    basic = rng.choice(selected, size=base_count, replace=False) if selected.size>base_count else selected
    contrast = np.max(rgb[selected],axis=1).astype(np.int16) - np.min(rgb[selected],axis=1).astype(np.int16)
    # Oversample colored regions: biology figures contain huge pale backgrounds
    # but important tiny red/orange/purple structures.
    colored = selected[contrast > 42]
    color_count = min(colored.size, sample_limit//4)
    extra = rng.choice(colored, size=color_count, replace=False) if color_count else np.empty(0,dtype=np.int64)
    # Equal sampling by image grid prevents small spatial clusters being lost.
    grid = np.arange(0,len(rgb),max(1,len(rgb)//max(1,sample_limit//4)),dtype=np.int64)
    grid=grid[foreground[grid]]
    sample=np.concatenate([basic,extra,grid]).astype(np.int64)
    sample_lab = np.ascontiguousarray(lab[sample], dtype=np.float32)
    # OpenCV's RNG is global, but a fixed seed makes this batch deterministic.
    cv2.setRNGSeed(19)
    _,_,centers = cv2.kmeans(sample_lab, colors, None,
                  (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 22, .25),
                  2, cv2.KMEANS_PP_CENTERS)
    # Map each pixel in bounded chunks; no N*color*3 whole-image allocation.
    labels = np.full(len(rgb), -1, dtype=np.int16)
    for sub in np.array_split(selected, max(1,int(np.ceil(len(selected)/35000)))):
        distances = ((lab[sub,None,:]-centers[None,:,:])**2).sum(axis=2)
        labels[sub] = np.argmin(distances,axis=1).astype(np.int16)
    # Refit output colors in *original RGB* to minimize per-cluster reconstruction
    # bias from LAB conversion and avoid artificial gray casts.
    pal = np.zeros((colors,3),dtype=np.uint8)
    src = rgb[selected].astype(np.float64)
    used=labels[selected].astype(np.int32)
    counts=np.bincount(used,minlength=colors)
    for ch in range(3):
        sums=np.bincount(used,weights=src[:,ch],minlength=colors)
        pal[:,ch]=np.clip(np.rint(sums/np.maximum(1,counts)),0,255).astype(np.uint8)
    return labels.reshape(h,w),pal


def build_color_plan(img: np.ndarray, colors: int, mode: str='perceptual',
                     use_gradient: bool=True, luminance_weight: float=1.0) -> ColorPlan:
    if img.dtype != np.uint8 or img.ndim != 3 or img.shape[-1] != 3:
        raise ValueError('ColorPlan expects uint8 RGB pixels')
    if mode not in ('legacy','balanced','perceptual','fusion'):
        raise ValueError('color mode must be legacy, balanced, perceptual or fusion')
    gradient_rows,bg_mask,gradient=estimate_background(img,enabled=use_gradient)
    if mode in ('legacy','balanced'):
        from PIL import Image
        quant=Image.fromarray(img,'RGB').quantize(colors=colors,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE)
        ids=np.asarray(quant,dtype=np.int16).copy()
        palette=np.asarray(quant.getpalette(),dtype=np.uint8).reshape(-1,3).copy()
        if mode == 'balanced':
            # Retain MEDIANCUT's good luminance/edge topology, but estimate the
            # actual median original color of every segment. This reduces RGB
            # color bias without changing any mask edge or object count.
            for value in np.unique(ids):
                palette[int(value)] = np.median(img[ids==value],axis=0).astype(np.uint8)
        # Convert the palette background pixels to a gradient only when the
        # actual rendered RGB was within threshold of our fitted background.
        ids[bg_mask]=-1
    elif mode == 'fusion':
        from PIL import Image
        # Half of the palette preserves neutral/edge tone levels, the other half
        # guarantees chromatic colors aren't collapsed into blue-gray.
        neutral_k=max(2,colors//2)
        vivid_k=max(2,colors-neutral_k)
        neutral=Image.fromarray(img,'RGB').quantize(colors=neutral_k,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE)
        neutral_pal=np.asarray(neutral.getpalette(),dtype=np.uint8).reshape(-1,3)[:neutral_k]
        _,chromatic_pal=perceptual_palette(img,vivid_k,bg_mask,luminance_weight=luminance_weight)
        pal=np.concatenate([neutral_pal,chromatic_pal],axis=0)
        lab=_rgb_to_lab(img)
        colorlab=_rgb_to_lab(pal)
        lab[:,0]*=luminance_weight
        colorlab[:,0]*=luminance_weight
        flat=np.flatnonzero((~bg_mask).ravel())
        mapped=np.full(len(lab),-1,dtype=np.int16)
        for chunk in np.array_split(flat,max(1,int(np.ceil(len(flat)/40000)))):
            dist=((lab[chunk,None,:]-colorlab[None,:,:])**2).sum(axis=2)
            mapped[chunk]=np.argmin(dist,axis=1)
        ids=mapped.reshape(img.shape[:2])
        palette=pal.copy()
        orig=img.reshape(-1,3)
        for label in np.unique(mapped[flat]):
            values=orig[flat[mapped[flat]==label]]
            palette[int(label)]=np.median(values,axis=0).astype('uint8')
    else:
        ids,palette=perceptual_palette(img,colors,bg_mask,luminance_weight=luminance_weight)
    color_values=palette[np.maximum(ids,0)]
    reconstructed=np.where((ids<0)[...,None],gradient_rows[:,None,:],color_values).astype(np.uint8)
    delta=np.abs(img.astype(np.float32)-reconstructed.astype(np.float32))
    mean_mae=float(delta.mean())
    ref_lab=_rgb_to_lab(img)
    rec_lab=_rgb_to_lab(reconstructed)
    delta_e=np.linalg.norm(ref_lab-rec_lab,axis=1)
    diagnostics={'palette_mode':mode,'background_gradient':bool(gradient),
                 'background_coverage':round(float(bg_mask.mean()),4),
                 'quantized_rgb_mae':round(mean_mae,4),
                 'quantized_delta_e76_mean':round(float(delta_e.mean()),4),
                 'quantized_delta_e76_p95':round(float(np.percentile(delta_e,95)),4),
                 'palette_size':len(palette),'luminance_weight':luminance_weight}
    color=tuple(int(x) for x in np.rint(np.mean(gradient_rows,axis=0)))
    return ColorPlan(ids,palette,gradient,color,diagnostics)
