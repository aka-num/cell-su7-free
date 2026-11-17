"""Pure-Python SVG geometry and PowerPoint custom paths, no cloud services.
MIT-licensed project implementation. Lengths/coordinates are in source viewBox units.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math, re
from typing import Iterable
from xml.etree import ElementTree as ET

NUM=r'[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?'
COMMANDS=re.compile(r'[AaCcHhLlMmQqSsTtVvZz]|'+NUM)
IDENT=(1.,0.,0.,1.,0.,0.)

@dataclass
class Path:
    # commands M(x,y), L(x,y), C(x1,y1,x2,y2,x3,y3), Z()
    commands: list[tuple[str,tuple[float,...]]]=field(default_factory=list)

    def transformed(self,m):
        out=Path()
        for op,args in self.commands:
            if op=='Z':out.commands.append((op,()));continue
            xy=[]
            for x,y in zip(args[::2],args[1::2]):xy.extend(apply_matrix(m,x,y))
            out.commands.append((op,tuple(xy)))
        return out


def apply_matrix(m,x,y):
    a,b,c,d,e,f=m;return (a*x+c*y+e,b*x+d*y+f)

def mul(p,q):
    a,b,c,d,e,f=p;g,h,i,j,k,l=q
    return (a*g+c*h,b*g+d*h,a*i+c*j,b*i+d*j,a*k+c*l+e,b*k+d*l+f)

def transform_parse(text):
    t=IDENT
    for name,arg in re.findall(r'([A-Za-z]+)\s*\(([^)]*)\)',text or ''):
        a=[float(v) for v in re.findall(NUM,arg)];n=name.lower()
        if n=='matrix' and len(a)==6:m=tuple(a)
        elif n=='translate' and a:m=(1,0,0,1,a[0],a[1] if len(a)>1 else 0)
        elif n=='scale' and a:m=(a[0],0,0,a[1] if len(a)>1 else a[0],0,0)
        elif n=='rotate' and a:
            angle=math.radians(a[0]);c=math.cos(angle);s=math.sin(angle);m=(c,s,-s,c,0,0)
            if len(a)>=3:m=mul(mul((1,0,0,1,a[1],a[2]),m),(1,0,0,1,-a[1],-a[2]))
        elif n=='skewx' and a:m=(1,0,math.tan(math.radians(a[0])),1,0,0)
        elif n=='skewy' and a:m=(1,math.tan(math.radians(a[0])),0,1,0,0)
        else:continue
        t=mul(t,m)
    return t

def arc_to_cubics(x1,y1,rx,ry,angle,large,sweep,x2,y2):
    """SVG elliptical arc to <=90 degree cubic segments (SVG 1.1 endpoint algorithm)."""
    if rx==0 or ry==0 or (x1==x2 and y1==y2):return [('L',(x2,y2))]
    rx,ry=abs(rx),abs(ry);phi=math.radians(angle%360);co,si=math.cos(phi),math.sin(phi)
    dx,dy=(x1-x2)/2,(y1-y2)/2
    xp,yp=co*dx+si*dy,-si*dx+co*dy
    lam=xp*xp/(rx*rx)+yp*yp/(ry*ry)
    if lam>1:rx*=math.sqrt(lam);ry*=math.sqrt(lam)
    numerator=max(0.,(rx*rx*ry*ry-rx*rx*yp*yp-ry*ry*xp*xp))
    denominator=rx*rx*yp*yp+ry*ry*xp*xp
    coef=(-1 if bool(large)==bool(sweep) else 1)*math.sqrt(numerator/denominator) if denominator else 0
    cxp=coef*rx*yp/ry;cyp=-coef*ry*xp/rx
    cx=co*cxp-si*cyp+(x1+x2)/2;cy=si*cxp+co*cyp+(y1+y2)/2
    def theta(u,v):return math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])
    u=((xp-cxp)/rx,(yp-cyp)/ry);v=((-xp-cxp)/rx,(-yp-cyp)/ry)
    start=theta((1,0),u);delta=theta(u,v)
    if not sweep and delta>0:delta-=2*math.pi
    if sweep and delta<0:delta+=2*math.pi
    count=max(1,math.ceil(abs(delta)/(math.pi/2)));dt=delta/count
    def point(x,y):return (cx+rx*co*x-ry*si*y,cy+rx*si*x+ry*co*y)
    out=[]
    for i in range(count):
        t0=start+i*dt;t1=t0+dt;k=4/3*math.tan(dt/4)
        u0=(math.cos(t0),math.sin(t0));u1=(math.cos(t1),math.sin(t1))
        cp1=point(u0[0]-k*u0[1],u0[1]+k*u0[0]);cp2=point(u1[0]+k*u1[1],u1[1]-k*u1[0]);end=point(*u1)
        out.append(('C',(*cp1,*cp2,*end)))
    return out

def parse_path(d):
    tok=COMMANDS.findall(d or '');p=Path();i=0;cmd=None;x=y=0.;sx=sy=0.;last_c=None;last_q=None;prev=''
    argc={'M':2,'L':2,'H':1,'V':1,'C':6,'S':4,'Q':4,'T':2,'A':7}
    while i<len(tok):
        if re.fullmatch('[A-Za-z]',tok[i]):
            cmd=tok[i];i+=1
            if cmd in 'Zz':p.commands.append(('Z',()));x,y=sx,sy;last_c=last_q=None;prev='Z';cmd=None;continue
        if not cmd:raise ValueError('SVG path missing command')
        op=cmd.upper();n=argc.get(op)
        if n is None:raise ValueError(f'unsupported SVG command: {cmd}')
        if i+n>len(tok) or any(re.fullmatch('[A-Za-z]',v) for v in tok[i:i+n]):raise ValueError(f'incomplete SVG path {cmd}')
        v=list(map(float,tok[i:i+n]));i+=n;relative=cmd.islower();ox,oy=x,y
        def end(a,b):return (a+x,b+y) if relative else (a,b)
        if op=='M':x,y=end(*v);p.commands.append(('M',(x,y)));sx,sy=x,y;cmd='l' if relative else 'L'
        elif op=='L':x,y=end(*v);p.commands.append(('L',(x,y)))
        elif op=='H':x=(x+v[0]) if relative else v[0];p.commands.append(('L',(x,y)))
        elif op=='V':y=(y+v[0]) if relative else v[0];p.commands.append(('L',(x,y)))
        elif op=='C':
            a,b=end(v[0],v[1]);c,d=end(v[2],v[3]);x,y=end(v[4],v[5]);p.commands.append(('C',(a,b,c,d,x,y)));last_c=(c,d)
        elif op=='S':
            a,b=(2*x-last_c[0],2*y-last_c[1]) if prev in ('C','S') and last_c else (x,y)
            c,d=end(v[0],v[1]);x,y=end(v[2],v[3]);p.commands.append(('C',(a,b,c,d,x,y)));last_c=(c,d)
        elif op=='Q':
            a,b=end(v[0],v[1]);x,y=end(v[2],v[3]);p.commands.append(('C',(ox+2*(a-ox)/3,oy+2*(b-oy)/3,x+2*(a-x)/3,y+2*(b-y)/3,x,y)));last_q=(a,b)
        elif op=='T':
            a,b=(2*x-last_q[0],2*y-last_q[1]) if prev in ('Q','T') and last_q else (x,y)
            x,y=end(v[0],v[1]);p.commands.append(('C',(ox+2*(a-ox)/3,oy+2*(b-oy)/3,x+2*(a-x)/3,y+2*(b-y)/3,x,y)));last_q=(a,b)
        elif op=='A':
            x,y=end(v[5],v[6]);p.commands+=arc_to_cubics(ox,oy,v[0],v[1],v[2],int(v[3]),int(v[4]),x,y)
        if op not in ('C','S'):last_c=None
        if op not in ('Q','T'):last_q=None
        prev=op
    return p

def polygon(points,closed=True):
    p=Path()
    for i,(x,y) in enumerate(points):p.commands.append(('M' if i==0 else 'L',(float(x),float(y))))
    if closed and points:p.commands.append(('Z',()))
    return p

def ellipse(cx,cy,rx,ry):
    k=.5522847498307936
    return Path([('M',(cx+rx,cy)),('C',(cx+rx,cy+k*ry,cx+k*rx,cy+ry,cx,cy+ry)),('C',(cx-k*rx,cy+ry,cx-rx,cy+k*ry,cx-rx,cy)),('C',(cx-rx,cy-k*ry,cx-k*rx,cy-ry,cx,cy-ry)),('C',(cx+k*rx,cy-ry,cx+rx,cy-k*ry,cx+rx,cy)),('Z',())])

def path_bbox(p):
    pts=[(x,y) for _,args in p.commands for x,y in zip(args[::2],args[1::2])]
    if not pts:return (0.,0.,0.,0.)
    return (min(x for x,y in pts),min(y for x,y in pts),max(x for x,y in pts),max(y for x,y in pts))


def _flatten_subpath(commands, steps=12):
    """Approximate a closed subpath for winding/containment only; exported curves stay exact."""
    points=[];here=None
    for op, args in commands:
        if op=='M':
            here=(args[0],args[1]);points.append(here)
        elif op=='L':
            here=(args[0],args[1]);points.append(here)
        elif op=='C' and here is not None:
            x0,y0=here;x1,y1,x2,y2,x3,y3=args
            for i in range(1,steps+1):
                t=i/steps;u=1-t
                points.append((u*u*u*x0+3*u*u*t*x1+3*u*t*t*x2+t*t*t*x3,
                               u*u*u*y0+3*u*u*t*y1+3*u*t*t*y2+t*t*t*y3))
            here=(x3,y3)
    return points


def _area(poly):
    return sum(x1*y2-x2*y1 for (x1,y1),(x2,y2) in zip(poly,poly[1:]+poly[:1]))/2 if len(poly)>2 else 0


def _contains(poly,px,py):
    inside=False
    for (x1,y1),(x2,y2) in zip(poly,poly[1:]+poly[:1]):
        if (y1>py)!=(y2>py) and px < (x2-x1)*(py-y1)/(y2-y1)+x1:
            inside=not inside
    return inside


def _reverse_closed_subpath(commands):
    """Reverse polygon/Cubic winding without approximating the control geometry."""
    if not commands or commands[0][0]!='M' or commands[-1][0]!='Z':return commands
    origin=commands[0][1];here=origin;edges=[]
    for op,a in commands[1:-1]:
        if op=='L':edges.append(('L',here,a));here=a
        elif op=='C':edges.append(('C',here,a));here=a[-2:]
    if not edges:return commands
    out=[('M',here)]
    for op,start,args in reversed(edges):
        if op=='L':out.append(('L',start))
        else:out.append(('C',(args[2],args[3],args[0],args[1],start[0],start[1])))
    out.append(('Z',()))
    return out


def normalize_evenodd(path):
    """Convert SVG evenodd subpaths to alternating nonzero winding.

    OOXML has no reliable evenodd custom-geometry switch across PowerPoint
    versions. Nested contours with alternating winding preserve common holes.
    Intersecting/self-intersecting contours remain an explicit limitation.
    """
    subpaths=[];current=[]
    for item in path.commands:
        if item[0]=='M' and current:
            subpaths.append(current);current=[]
        current.append(item)
    if current:subpaths.append(current)
    polys=[_flatten_subpath(c) if c and c[-1][0]=='Z' else [] for c in subpaths]
    areas=[_area(p) for p in polys]
    result=[]
    for i,commands in enumerate(subpaths):
        if polys[i] and abs(areas[i])>1e-9:
            # The start vertex is inside all enclosing (non-touching) contours.
            px,py=polys[i][0]
            depth=sum(1 for j,p in enumerate(polys) if j!=i and p and
                      abs(areas[j])>abs(areas[i])+1e-7 and _contains(p,px,py))
            want_positive=(depth%2==0)
            if (areas[i]>0)!=want_positive:commands=_reverse_closed_subpath(commands)
        result+=commands
    return Path(result)
