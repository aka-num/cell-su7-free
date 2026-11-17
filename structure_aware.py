"""Conservative, deterministic geometric reconstruction of isolated raster components.

No neural/semantic guesses. A primitive is accepted only after measuring pixel-mask
IoU against the connected component. Nonmatching biology-like artwork stays as a
high-fidelity editable outline. The original raster is never used as output art.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import cv2
import numpy as np
from geometry import polygon, ellipse, Path

@dataclass
class Proposal:
    kind: str
    primitive: str
    path: Path
    iou: float
    fill: bool = True
    stroke_width: float = 1.


def intersection_over_union(a: np.ndarray, b: np.ndarray) -> float:
    overlap = np.count_nonzero(a & b)
    union = np.count_nonzero(a | b)
    return float(overlap/union) if union else 0.


def fit_primitive(mask: np.ndarray, x: int, y: int, *, strict: float=.985) -> Proposal | None:
    """Find an editable primitive only if it reproduces the supplied pixel mask.

    x/y are the absolute coordinates of the *mask crop*, including its pixel
    cell origins. Pixel-aligned rectangles are exact. Curves are approximate and
    must satisfy strict mask overlap. Curved protein silhouettes are preserved.
    """
    mask = np.asarray(mask, dtype=np.bool_)
    if not mask.any():return None
    h,w=mask.shape
    yy,xx=np.nonzero(mask)
    x0=int(xx.min());x1=int(xx.max())+1;y0=int(yy.min());y1=int(yy.max())+1
    bw=x1-x0;bh=y1-y0;area=len(xx)
    if area<7:return None
    roi=mask[y0:y1,x0:x1]
    aspect=max(bw/max(bh,1),bh/max(bw,1))
    # Single-pixel-wide or completely filled bars are directly editable lines.
    if aspect>=5 and min(bw,bh)<=3 and np.all(roi):
        if bw>=bh:
            cy=y+y0+bh/2
            p=Path([('M',(float(x+x0),float(cy))),('L',(float(x+x1),float(cy)))])
        else:
            cx=x+x0+bw/2
            p=Path([('M',(float(cx),float(y+y0))),('L',(float(cx),float(y+y1)))])
        return Proposal('native_line','line',p,1.0,False,float(min(bw,bh)))
    if np.all(roi):
        return Proposal('native_rectangle','rect',polygon([(x+x0,y+y0),(x+x1,y+y0),
                                                            (x+x1,y+y1),(x+x0,y+y1)]),1.)
    if area<24 or min(bw,bh)<5 or max(bw,bh)>200:
        return None
    raw=roi.astype(np.uint8)*255
    contours,hierarchy=cv2.findContours(raw,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_NONE)
    # A filled ellipse/arrow cannot preserve a real transparent hole.
    if hierarchy is not None and any(int(h[3])>=0 for h in hierarchy[0]):
        return None
    if len(contours)!=1 or len(contours[0])<5:return None
    # Ellipse: accept only nearly solid, unoccluded elliptical objects. Full
    # contours must not have holes: their filled mask would change the artwork.
    if max(bw,bh)/min(bw,bh)<=4 and len(contours[0])>=5:
        (cx,cy),(diam1,diam2),angle=cv2.fitEllipse(contours[0])
        if min(diam1,diam2)>=3 and abs(angle)%90 <= 5:
            check=np.zeros(raw.shape,dtype=np.uint8)
            cv2.ellipse(check,((float(cx),float(cy)),(float(diam1),float(diam2)),float(angle)),255,-1)
            iou=intersection_over_union(roi,check>0)
            if iou>=strict:
                # For near-horizontal near-vertical ellipses choose axes, avoid
                # interpreting small cv2 numerical rotations as rotated geometry.
                if abs(angle-90)<5: rx,ry=diam2/2,diam1/2
                else:rx,ry=diam1/2,diam2/2
                p=ellipse(x+x0+cx+.5,y+y0+cy+.5,rx+.35,ry+.35)
                return Proposal('native_ellipse','ellipse',p,iou)
    # Conservative polygon promotion. Recognize broad arrow-like regions only
    # when outline has seven corners incl two concave shoulders; do not assign
    # biological meaning. Keep exact boundary path as fallback if uncertain.
    perimeter=cv2.arcLength(contours[0],True)
    approx=cv2.approxPolyDP(contours[0],max(0.8,perimeter*.011),True).reshape(-1,2)
    if len(approx)==7 and 0.18<area/(bw*bh)<0.85:
        pts=approx.astype(np.float64)+.5
        cross=[]
        for i in range(len(pts)):
            a=pts[i]-pts[i-1];b=pts[(i+1)%len(pts)]-pts[i]
            cross.append(float(a[0]*b[1]-a[1]*b[0]))
        neg=sum(v < -1 for v in cross);pos=sum(v>1 for v in cross)
        if min(pos,neg)>=2 and max(pos,neg)>=4:
            check=np.zeros(raw.shape,dtype=np.uint8)
            cv2.fillPoly(check,[approx.astype(np.int32)],255)
            iou=intersection_over_union(roi,check>0)
            if iou>=max(strict-.005, .980):
                pts_abs=[(float(x+x0+px),float(y+y0+py)) for px,py in pts]
                return Proposal('native_arrow_candidate','polygon',polygon(pts_abs),iou)
    return None


def color_similarity(a,b):
    """Euclidean RGB fallback for nearby quantized colors; no global recoloring."""
    return float(np.linalg.norm(np.array(a,dtype=float)-np.array(b,dtype=float)))


def repeated_groups(objects: list[dict], *, min_instances:int=3) -> list[dict]:
    """Stable, spatially aware grouping; members remain independently editable.

    Group means similar geometry and paint, NOT a biological class. Unlike old
    tile buckets, actual global object shapes can match across tile seams.
    """
    candidates=[]
    for o in objects:
        if o['kind'] not in ('native_ellipse','native_rectangle','native_line','native_arrow_candidate'):
            continue
        x0,y0,x1,y1=o['bbox'];w=x1-x0;h=y1-y0
        if w<3 or h<3 or w*h<16:continue
        candidates.append(o)
    # Greedy grouping capped per object to avoid quadratic large-diagram scans.
    remaining=candidates[:]
    groups=[]
    while remaining:
        seed=remaining.pop(0);similar=[seed];keep=[]
        x0,y0,x1,y1=seed['bbox'];sw=x1-x0;sh=y1-y0
        for o in remaining:
            a,b,c,d=o['bbox'];ow=c-a;oh=d-b
            if o['kind']==seed['kind'] and (abs(ow-sw)/max(sw,1)<.22 and
               abs(oh-sh)/max(sh,1)<.22 and color_similarity(o['rgb'],seed['rgb'])<28):
                similar.append(o)
            else:keep.append(o)
        remaining=keep
        if len(similar)>=min_instances:
            # Descriptors permit scene editor to select and move a whole group.
            members=[]
            for o in similar:
                a,b,c,d=o['bbox']
                members.append({'shape_index':o['shape_index'],'center':[(a+c)/2,(b+d)/2],
                                'bbox':o['bbox']})
            groups.append({'group_id':f'geometric_repeat_{len(groups)+1}',
                           'kind':'similar_geometry_not_semantics',
                           'prototype_shape_index':seed['shape_index'],
                           'member_shape_indices':[o['shape_index'] for o in similar],
                           'instances':members,'confidence':'geometry_only'})
    return groups


def detect_band_hints(img:np.ndarray) -> list[dict]:
    """Candidate long colored bands for manual annotation; NEVER rewrite pixels.

    Useful for locating membrane-like bands in scientific diagrams, but avoids
    falsely claiming a biological identity or assuming semantic connections.
    """
    h,w=img.shape[:2]
    if h<60 or w<100:return []
    hsv=cv2.cvtColor(img,cv2.COLOR_RGB2HSV)
    # Conservatively find saturated blue/cyan horizontal structures.
    blue=(hsv[:,:,0]>85)&(hsv[:,:,0]<132)&(hsv[:,:,1]>65)&(hsv[:,:,2]>35)
    kernel=cv2.getStructuringElement(cv2.MORPH_RECT,(max(9,int(w*.035)),5))
    closed=cv2.morphologyEx(blue.astype(np.uint8),cv2.MORPH_CLOSE,kernel)
    contours,_=cv2.findContours(closed,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    hints=[]
    for c in contours:
        x,y,bw,bh=cv2.boundingRect(c)
        if bw/w>=.5 and .025<=bh/h<=.37:
            coverage=float(np.count_nonzero(blue[y:y+bh,x:x+bw]))/max(bw*bh,1)
            if coverage<.10:continue
            # Approximate upper/lower surface tracks through blue-dominant
            # columns. Morphology hints are for human review, not for repainting.
            traces=[]
            for col in range(x,min(w,x+bw),max(5,int(w*.012))):
                ys=np.flatnonzero(blue[y:y+bh,col:col+3].any(axis=1))
                if len(ys)>3:
                    traces.append((col, y+int(np.percentile(ys,10)),
                                   y+int(np.percentile(ys,90))))
            if len(traces)>=5:
                tracks=np.array(traces,dtype=float)
                for i in (1,2):
                    for j in range(2,len(tracks)-2):
                        tracks[j,i]=np.median([t[i] for t in traces[j-2:j+3]])
                top=[[int(a),int(b)] for a,b,_ in tracks]
                bottom=[[int(a),int(c)] for a,_,c in tracks]
            else:top=[];bottom=[]
            hints.append({'kind':'elongated_blue_band_candidate','bbox':[x,y,x+bw,y+bh],
                          'blue_fraction':round(coverage,4),
                          'upper_track':top,'lower_track':bottom,
                          'note':'Visual band only; may or may not represent a membrane.'})
    return sorted(hints,key=lambda a:a['blue_fraction'],reverse=True)[:4]
