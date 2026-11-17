"""Local visual editor: live original/vector comparison and *persistent* OOXML edits.

All geometry remains native editable paths/text. No upload or model invocation.
Tk receives only precomputed preview images; the underlying scene is the source
of truth and is modified by every drag. Offsets are validated at export time.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import math
import queue
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk
from geometry import path_bbox
from engine import Scene, Settings, write_svg


def source_signature(source: Path, settings: Settings) -> dict:
    """Reject edits from other images/vectorization settings (including inpaint)."""
    s=Path(source)
    subset={k:getattr(settings,k) for k in ('colors','min_area','simplify','max_pixels',
          'max_shapes','backend','precision_tile','precision_max_commands','precision_separate_regions','precision_min_region_area',
          'color_mode','luminance_weight','background_gradient','structure_mode','manifest_clean','inpaint','inpaint_radius','ai_mode','ocr','group_regions')}
    # Non-cleaning runs have no OCR mask, so the inpaint toggle has no
    # effect. Normalizing it also makes saved GUI edits usable from CLI.
    if not settings.manifest_clean and not settings.ocr and settings.ai_mode=='off':
        subset['inpaint']=False
        subset['inpaint_radius']=None
    return {'sha256': sha256(s.read_bytes()).hexdigest(), 'settings': subset}


def _rect_close(a,b):
    return len(a)==len(b)==4 and all(abs(x-y)<=max(.5,abs(y)*.002) for x,y in zip(a,b))


def _translate_shape(scene: Scene,index:int,dx:float,dy:float):
    shape=scene.shapes[index]
    shape.path=shape.path.transformed((1,0,0,1,dx,dy))
    if shape.gradient:
        # Native OOXML gradient stays in the translated shape's coordinate space.
        pass


def apply_edit_plan(scene: Scene,plan:dict,identity:dict):
    """Check *all* anchors first: never silently move a different vector object."""
    if not plan:return scene
    if plan.get('version')!=1 or plan.get('identity')!=identity:
        raise ValueError('可视化编辑与当前原图或矢量化参数不匹配，请重新打开画布编辑。')
    if plan.get('shape_count')!=len(scene.shapes):
        raise ValueError('图形数量已改变，旧画布位移不再适用，请重新编辑。')
    operations=[]
    for item in plan.get('shape_moves',[]):
        index=item.get('index')
        if type(index) is not int or not 0<=index<len(scene.shapes):raise ValueError('编辑数据包含无效图形编号')
        if not _rect_close(path_bbox(scene.shapes[index].path),item.get('origin_bbox',[])):
            raise ValueError(f'图形 #{index+1} 的原始位置已变化，不能安全复用旧编辑')
        dx=float(item['dx']);dy=float(item['dy'])
        if not all(math.isfinite(x) and abs(x)<=max(scene.width,scene.height)*2 for x in (dx,dy)):
            raise ValueError('编辑数据包含异常位移')
        operations.append(('shape',index,dx,dy))
    for item in plan.get('native_text_moves',[]):
        index=item.get('index')
        if type(index) is not int or not 0<=index<len(scene.texts):raise ValueError('编辑数据包含无效文本编号')
        t=scene.texts[index]
        if not _rect_close([t.x,t.y,t.x+1,t.y+1],
                [*item['origin_xy'],item['origin_xy'][0]+1,item['origin_xy'][1]+1]):
            raise ValueError('原生 SVG 文字位置已改变，旧位移不能安全复用')
        dx=float(item['dx']);dy=float(item['dy'])
        if not all(math.isfinite(x) and abs(x)<=max(scene.width,scene.height)*2 for x in (dx,dy)):
            raise ValueError('编辑数据包含异常文本位移')
        operations.append(('text',index,dx,dy))
    for kind,index,dx,dy in operations:
        if kind=='shape':_translate_shape(scene,index,dx,dy)
        else:scene.texts[index].x+=dx;scene.texts[index].y+=dy
    return scene


class EditableDocument:
    """Testable model backing both drag handling and editable PPTX export."""
    def __init__(self,scene:Scene,identity:dict,manifest:dict|None=None,previous:dict|None=None):
        self.scene=scene
        self.identity=identity
        self.manifest=deepcopy(manifest) if manifest else None
        self.manifest_start=len(scene.texts)-len(self.manifest['texts']) if self.manifest else len(scene.texts)
        self.moves={}
        self.native_moves={}
        self.shape_origins={}
        self.text_origins={}
        graph=getattr(scene,'structure_graph',{}) or {}
        # Groups are geometry-only candidates. Their members remain individual
        # editable OOXML objects and are stored as per-object edit operations.
        self.groups={g['group_id']:tuple(i for i in g.get('member_shape_indices',[])
                     if type(i) is int and 0<=i<len(scene.shapes))
                     for g in graph.get('repetition_groups',[])
                     if g.get('group_id') and len(g.get('member_shape_indices',[]))>=3}
        self.groups={k:v for k,v in self.groups.items() if len(v)>=3}

        self.history=[];self.future=[]
        self.changed_text_ids=set()
        if previous:
            apply_edit_plan(scene,previous,identity)
            for s in previous.get('shape_moves',[]):
                k=s['index'];self.moves[k]=[s['dx'],s['dy']];self.shape_origins[k]=s['origin_bbox']
            for t in previous.get('native_text_moves',[]):
                k=t['index'];self.native_moves[k]=[t['dx'],t['dy']];self.text_origins[k]=t['origin_xy']

    def text_manifest_id(self,index):
        idx=index-self.manifest_start
        if self.manifest and 0<=idx<len(self.manifest['texts']):
            return int(self.manifest['texts'][idx]['id'])
        return None

    def box(self,kind,index):
        if kind=='group':
            members=self.groups[index]
            boxes=[path_bbox(self.scene.shapes[i].path) for i in members]
            return (min(a for a,b,c,d in boxes),min(b for a,b,c,d in boxes),
                    max(c for a,b,c,d in boxes),max(d for a,b,c,d in boxes))
        if kind=='shape':return path_bbox(self.scene.shapes[index].path)
        text=self.scene.texts[index]
        if self.text_manifest_id(index) is not None:
            box=self.manifest['texts'][index-self.manifest_start]['bbox']
            return tuple(box)
        w=max(7.,len(text.content)*text.size*.57)
        return (text.x,text.y-text.size*1.12,text.x+w,text.y+text.size*.2)

    def select(self,x,y,*,kind_filter='all'):
        if kind_filter=='group':
            matches=[]
            for group_id in self.groups:
                a,b,c,d=self.box('group',group_id)
                if a-4<=x<=c+4 and b-4<=y<=d+4:
                    matches.append(((c-a)*(d-b),group_id))
            if matches:return ('group',min(matches)[1])
            return None
        # Text wins over intersecting shapes; choose the smallest matching path,
        # not the huge background rectangle, when the cursor hits many objects.
        if kind_filter in ('all','text'):
            for i in reversed(range(len(self.scene.texts))):
                a,b,c,d=self.box('text',i)
                if a-4<=x<=c+4 and b-4<=y<=d+4:return ('text',i)
        if kind_filter in ('all','shape'):
            matches=[]
            for i in range(len(self.scene.shapes)):
                a,b,c,d=self.box('shape',i)
                if a-3<=x<=c+3 and b-3<=y<=d+3:
                    area=max(1,c-a)*max(1,d-b)
                    matches.append((area,-i,i))
            if matches:return ('shape',min(matches)[2])
        return None

    def _shift(self,kind,index,dx,dy):
        if not all(math.isfinite(v) for v in (dx,dy)):raise ValueError('位移必须是有限数字')
        if kind=='group':
            if index not in self.groups:raise ValueError('未知的结构分组')
            for shape_id in self.groups[index]:self._shift('shape',shape_id,dx,dy)
            return
        if kind=='shape':
            if index not in self.shape_origins:
                self.shape_origins[index]=list(self.box('shape',index))
            _translate_shape(self.scene,index,dx,dy)
            moved=self.moves.setdefault(index,[0.,0.]);moved[0]+=dx;moved[1]+=dy
        elif kind=='text':
            t=self.scene.texts[index]
            manifest_id=self.text_manifest_id(index)
            if manifest_id is not None:
                entry=self.manifest['texts'][index-self.manifest_start]
                b=entry['bbox'];w,h=self.scene.width,self.scene.height
                if b[0]+dx<0 or b[2]+dx>w or b[1]+dy<0 or b[3]+dy>h:
                    raise ValueError('文字框不可拖出原图范围')
                entry['bbox']=[round(v+(dx if j%2==0 else dy),3) for j,v in enumerate(b)]
                entry['position']=[entry['bbox'][0],entry['bbox'][3]]
                entry['reviewed']=False
                self.changed_text_ids.add(manifest_id)
            else:
                if index not in self.text_origins:
                    self.text_origins[index]=[t.x,t.y]
                moved=self.native_moves.setdefault(index,[0.,0.]);moved[0]+=dx;moved[1]+=dy
            t.x+=dx;t.y+=dy
        else:raise ValueError('Unknown object type')

    def move(self,kind,index,dx,dy):
        dx=float(dx);dy=float(dy)
        if abs(dx)+abs(dy)<1e-7:return
        if abs(dx)>self.scene.width or abs(dy)>self.scene.height:raise ValueError('单次位移过大')
        self._shift(kind,index,dx,dy)
        self.history.append((kind,index,dx,dy))
        self.future.clear()

    def undo(self):
        if not self.history:return False
        k,i,dx,dy=self.history.pop()
        self._shift(k,i,-dx,-dy)
        self.future.append((k,i,dx,dy))
        return True

    def redo(self):
        if not self.future:return False
        k,i,dx,dy=self.future.pop()
        self._shift(k,i,dx,dy)
        self.history.append((k,i,dx,dy))
        return True

    def plan(self):
        return {'version':1,'identity':self.identity,'shape_count':len(self.scene.shapes),
            'shape_moves':[{'index':i,'origin_bbox':self.shape_origins[i],
                            'dx':round(v[0],5),'dy':round(v[1],5)}
                           for i,v in sorted(self.moves.items()) if abs(v[0])+abs(v[1])>1e-5],
            'ocr_text_ids':sorted(self.changed_text_ids),
            'native_text_moves':[{'index':i,'origin_xy':self.text_origins[i],
                                  'dx':round(v[0],5),'dy':round(v[1],5)}
                                 for i,v in sorted(self.native_moves.items()) if abs(v[0])+abs(v[1])>1e-5]}


def rasterize_scene(scene,canvas_width:int,canvas_height:int)->Image.Image:
    """Render exactly the SVG used for editable export; never fake reconstruction."""
    import cairosvg
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'preview.svg';write_svg(scene,p)
        blob=cairosvg.svg2png(url=str(p),output_width=int(canvas_width),output_height=int(canvas_height))
    return Image.open(BytesIO(blob)).convert('RGB')


class VisualEditor(tk.Toplevel):
    """Side-by-side original/rebuilt views; live hit-testing, drag, undo/redo."""
    def __init__(self,master,original:Image.Image,doc:EditableDocument,on_save):
        super().__init__(master)
        self.title('可视化编辑画布 · 拖动物体 / 文字 · 离线')
        self.geometry('1140x730');self.minsize(540,390)
        self.transient(master)
        self.doc=doc;self.original=original.convert('RGB')
        self.rebuilt=None
        self.callback=on_save
        self.zoom=tk.DoubleVar(value=1.)
        self.overlay=tk.BooleanVar(value=False)
        self.opacity=tk.DoubleVar(value=.5)
        self.filter=tk.StringVar(value='all')
        self.message=tk.StringVar(value='左：原图。右：实际 SVG 重建，可拖动文本框和矢量对象。滚轮缩放；中键拖动平移。')
        self.selection=None;self.dragstart=None;self._drag_outline=None
        self.pan=[0.,0.];self._render_serial=0
        self._render_results=queue.Queue()
        self._build()
        self.protocol('WM_DELETE_WINDOW',self.destroy)
        self.bind('<Control-z>',lambda e:self._step(-1))
        self.bind('<Control-y>',lambda e:self._step(1))
        self._render_after_id=self.after(30,self._start_render)
        self._poll_after_id=self.after(60,self._poll_render)
        self.grab_set()

    def _build(self):
        bar=ttk.Frame(self,padding=8);bar.pack(fill='x')
        ttk.Button(bar,text='−',width=3,command=lambda:self._set_zoom(self.zoom.get()/1.2)).pack(side='left')
        ttk.Scale(bar,from_=.3,to=4.,variable=self.zoom,command=lambda _=None:self._redraw()).pack(side='left',fill='x',expand=True,padx=7)
        ttk.Button(bar,text='+',width=3,command=lambda:self._set_zoom(self.zoom.get()*1.2)).pack(side='left')
        ttk.Button(bar,text='适应',command=lambda:self._set_zoom(1.)).pack(side='left',padx=(8,0))
        ttk.Checkbutton(bar,text='叠加对比',variable=self.overlay,command=self._redraw).pack(side='left',padx=(15,5))
        ttk.Scale(bar,from_=0,to=1,variable=self.opacity,command=lambda _=None:self._redraw(),length=100).pack(side='left')
        sel=ttk.Frame(self,padding=(9,0));sel.pack(fill='x')
        ttk.Label(sel,text='选择').pack(side='left')
        ttk.Combobox(sel,textvariable=self.filter,values=['all','text','shape','group'],state='readonly',width=7).pack(side='left',padx=8)
        ttk.Button(sel,text='撤销 Ctrl+Z',command=lambda:self._step(-1)).pack(side='left')
        ttk.Button(sel,text='重做 Ctrl+Y',command=lambda:self._step(1)).pack(side='left',padx=8)
        self.panes=ttk.Panedwindow(self,orient='horizontal');self.panes.pack(fill='both',expand=True,padx=8,pady=7)
        self.views=[]
        for title in ('原始图片 · 只读参考','重建结果 · 拖动编辑'):
            frame=ttk.Frame(self.panes)
            ttk.Label(frame,text=title).pack(anchor='w',pady=(0,5))
            canvas=tk.Canvas(frame,bg='#cdd5e4',highlightthickness=0,cursor='crosshair')
            canvas.pack(fill='both',expand=True)
            self.panes.add(frame,weight=1)
            self.views.append(canvas)
            canvas.bind('<Configure>',lambda e:self._redraw())
            canvas.bind('<MouseWheel>',self._on_wheel)
            canvas.bind('<Button-4>',self._on_wheel)
            canvas.bind('<Button-5>',self._on_wheel)
            canvas.bind('<ButtonPress-2>',self._pan_begin)
            canvas.bind('<B2-Motion>',self._pan_drag)
            canvas.bind('<ButtonPress-3>',self._pan_begin)
            canvas.bind('<B3-Motion>',self._pan_drag)
        self.views[1].bind('<ButtonPress-1>',self._press)
        self.views[1].bind('<B1-Motion>',self._drag)
        self.views[1].bind('<ButtonRelease-1>',self._release)
        self.bind('<Left>',lambda e:self._nudge_pan(30,0))
        self.bind('<Right>',lambda e:self._nudge_pan(-30,0))
        self.bind('<Up>',lambda e:self._nudge_pan(0,30))
        self.bind('<Down>',lambda e:self._nudge_pan(0,-30))
        foot=ttk.Frame(self,padding=9);foot.pack(fill='x')
        action_row=ttk.Frame(foot);action_row.pack(side='top',fill='x')
        ttk.Button(action_row,text='取消',command=self.destroy).pack(side='right',padx=5)
        ttk.Button(action_row,text='保存画布编辑',command=self._save).pack(side='right')
        info=ttk.Label(foot,textvariable=self.message,wraplength=800)
        info.pack(side='top',fill='x',pady=(7,0))
        def sized(event):
            if event.widget is self:info.configure(wraplength=max(200,event.width-35))
        self.bind('<Configure>',sized,add='+')

    def _start_render(self):
        self._render_serial+=1;serial=self._render_serial
        self.message.set('正在本地渲染真实 SVG 预览，不调用 API…')
        frozen_scene=deepcopy(self.doc.scene)
        def worker():
            try:
                im=rasterize_scene(frozen_scene,self.original.width,self.original.height)
                self._render_results.put((serial,im,None))
            except Exception as exc:
                self._render_results.put((serial,None,str(exc)))
        threading.Thread(target=worker,daemon=True).start()

    def _poll_render(self):
        if not self.winfo_exists():return
        while True:
            try:serial,im,error=self._render_results.get_nowait()
            except queue.Empty:break
            self._render_done(serial,im,error)
        self._poll_after_id=self.after(60,self._poll_render)

    def _render_done(self,serial,im,error):
        if not self.winfo_exists() or serial!=self._render_serial:return
        if error:
            self.message.set('SVG 预览失败：'+error)
            return
        self.rebuilt=im
        self.message.set('可编辑预览已加载。右图点击选择文字/图形，拖动后将修改实际矢量对象。')
        self._redraw()

    def _set_zoom(self,value):
        self.zoom.set(max(.3,min(4.,value)));self._redraw()

    def _transform(self,canvas):
        w,h=max(100,canvas.winfo_width()),max(100,canvas.winfo_height())
        fit=min((w-22)/self.original.width,(h-22)/self.original.height)
        scale=max(.01,fit*self.zoom.get())
        return ((w-self.original.width*scale)/2+self.pan[0],
                (h-self.original.height*scale)/2+self.pan[1],scale)

    def _scene_xy(self,canvas,x,y):
        ox,oy,k=self._transform(canvas)
        return ((x-ox)/k*self.doc.scene.width/self.original.width,
                (y-oy)/k*self.doc.scene.height/self.original.height)

    def _screen_rect(self,canvas,box):
        ox,oy,k=self._transform(canvas)
        x0,y0,x1,y1=box
        return (ox+x0*k*self.original.width/self.doc.scene.width,
                oy+y0*k*self.original.height/self.doc.scene.height,
                ox+x1*k*self.original.width/self.doc.scene.width,
                oy+y1*k*self.original.height/self.doc.scene.height)

    def _redraw(self):
        if not self.winfo_exists():return
        for i,c in enumerate(self.views):
            c.delete('all')
            im=self.original if i==0 else self.rebuilt
            if im is None:
                c.create_text(max(40,c.winfo_width()/2),max(30,c.winfo_height()/2),text='正在生成预览…',fill='#192d48')
                continue
            if i==1 and self.overlay.get():
                im=Image.blend(self.original,self.rebuilt,self.opacity.get())
            ox,oy,k=self._transform(c)
            dest=(max(1,round(im.width*k)),max(1,round(im.height*k)))
            # Avoid huge Tk photos; zoom is capped and canvas clipping is provided.
            if dest[0]*dest[1]>22_000_000:
                c.create_text(40,40,text='缩放过大，请缩小',anchor='nw',fill='#d31c3d')
                continue
            photo=ImageTk.PhotoImage(im.resize(dest,Image.Resampling.BILINEAR))
            c._photo=photo
            c.create_image(ox,oy,image=photo,anchor='nw')
        if self.selection:self._draw_selected()

    def _draw_selected(self,shift=(0.,0.)):
        if not self.selection:return
        c=self.views[1]
        kind,index=self.selection
        coords=list(self._screen_rect(c,self.doc.box(kind,index)))
        coords=[v+ (shift[0] if j%2==0 else shift[1]) for j,v in enumerate(coords)]
        self._drag_outline=c.create_rectangle(*coords,outline='#ed2255',width=3,dash=(5,3),tags='selection')
        c.create_text(coords[0],coords[1]-6,text=f'{kind} #{index+1}' if type(index) is int else f'{kind}: {index}',fill='#ed2255',anchor='sw',tags='selection')

    def _press(self,event):
        if event.state & 0x0001: # Shift + left mouse: pan, including on trackpads
            self._pan_at=(event.x,event.y)
            self._shift_panning=True
            return
        self._shift_panning=False
        x,y=self._scene_xy(self.views[1],event.x,event.y)
        self.selection=self.doc.select(x,y,kind_filter=self.filter.get())
        self.dragstart=(event.x,event.y) if self.selection else None
        self._redraw()
        if self.selection:self.message.set(f'选中 {self.selection[0]} {self.selection[1]}；拖动将写入真实矢量坐标。结构组为几何相似候选，非生物学识别。')

    def _drag(self,event):
        if getattr(self,'_shift_panning',False):self._pan_drag(event);return
        if not self.dragstart or not self.selection:return
        c=self.views[1];c.delete('selection')
        self._draw_selected((event.x-self.dragstart[0],event.y-self.dragstart[1]))

    def _release(self,event):
        if getattr(self,'_shift_panning',False):
            self._shift_panning=False;return
        if not self.dragstart or not self.selection:return
        sx,sy=self.dragstart;self.dragstart=None
        if abs(event.x-sx)+abs(event.y-sy)<2:return
        c=self.views[1]
        a,b=self._scene_xy(c,sx,sy);x,y=self._scene_xy(c,event.x,event.y)
        try:self.doc.move(*self.selection,x-a,y-b)
        except Exception as exc:messagebox.showwarning('移动失败',str(exc),parent=self);self._redraw();return
        self._redraw();self._start_render()

    def _step(self,forward):
        done=self.doc.redo() if forward>0 else self.doc.undo()
        if done:self._redraw();self._start_render()

    def _on_wheel(self,event):
        if getattr(event,'num',None)==4:factor=1.13
        elif getattr(event,'num',None)==5:factor=1/1.13
        else:factor=1.13 if event.delta>0 else 1/1.13
        self._set_zoom(self.zoom.get()*factor)
        return 'break'

    def _nudge_pan(self,dx,dy):
        self.pan[0]+=dx;self.pan[1]+=dy;self._redraw()

    def _pan_begin(self,event):self._pan_at=(event.x,event.y)

    def _pan_drag(self,event):
        old=getattr(self,'_pan_at',(event.x,event.y))
        self.pan[0]+=event.x-old[0];self.pan[1]+=event.y-old[1]
        self._pan_at=(event.x,event.y);self._redraw()

    def destroy(self):
        for job in (getattr(self,'_render_after_id',None),getattr(self,'_poll_after_id',None)):
            if job:
                try:self.after_cancel(job)
                except tk.TclError:pass
        super().destroy()

    def _save(self):
        try:self.callback(self.doc)
        except Exception as exc:messagebox.showerror('保存画布失败',str(exc),parent=self);return
        self.destroy()
