"""Editable clipping of SVG path geometry against simple rectangular clip paths.

Uses open-source Shapely (BSD). Fully-contained curves are preserved exactly.
Only partly clipped shapes are polygonized; the resulting paths remain editable.
"""
from __future__ import annotations
from geometry import Path, polygon, _flatten_subpath, _area, _reverse_closed_subpath, path_bbox


def rect_clip(x,y,w,h,m):
    from shapely.geometry import Polygon
    from geometry import apply_matrix
    if w<=0 or h<=0:return Polygon()
    pts=[apply_matrix(m,xx,yy) for xx,yy in ((x,y),(x+w,y),(x+w,y+h),(x,y+h))]
    return Polygon(pts)


def _subpaths(path):
    out=[];cur=[]
    for cmd in path.commands:
        if cmd[0]=='M' and cur:out.append(cur);cur=[]
        cur.append(cmd)
    if cur:out.append(cur)
    return out


def _geometry_to_path(geometry,filled):
    from shapely.geometry import Polygon,MultiPolygon,LineString,MultiLineString,GeometryCollection
    output=[]
    def ring(coords,hole=False):
        pts=[(float(x),float(y)) for x,y in list(coords)[:-1]]
        if len(pts)<3:return
        signed=_area(pts)
        if (signed>0)==hole:pts.reverse()
        output.extend(polygon(pts).commands)
    def add(item):
        if item.is_empty:return
        if isinstance(item,Polygon):
            ring(item.exterior.coords)
            for hole in item.interiors:ring(hole.coords,True)
        elif isinstance(item,LineString):
            pts=[(float(x),float(y)) for x,y in item.coords]
            if len(pts)>=2:output.extend(polygon(pts,False).commands)
        elif isinstance(item,(MultiPolygon,MultiLineString,GeometryCollection)):
            for obj in item.geoms:add(obj)
    add(geometry)
    return Path(output)


def clip_vector(path,clip,filled=True):
    """Clip in source coordinates. Return path and whether it was tessellated."""
    from shapely.geometry import Polygon,LineString,GeometryCollection,box
    if clip.is_empty:return Path(),True
    x0,y0,x1,y1=path_bbox(path)
    if clip.covers(box(x0,y0,x1,y1)):
        # Bézier curves and original vertices remain untouched when fully inside.
        return path,False
    geom=None
    for subpath in _subpaths(path):
        points=_flatten_subpath(subpath,steps=32)
        if len(points)<2:continue
        if filled and subpath[-1][0]=='Z' and len(points)>=3:
            part=Polygon(points)
            if not part.is_valid:part=part.buffer(0)
            if part.is_empty:continue
            geom=part if geom is None else geom.symmetric_difference(part)
        else:
            part=LineString(points)
            geom=part if geom is None else geom.union(part)
    if geom is None:return Path(),True
    try:cut=geom.intersection(clip)
    except Exception:return path,False
    return _geometry_to_path(cut,filled),True
