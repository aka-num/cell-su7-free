"""Single-window, task-oriented GUI for Cell SU7 Remastered.

Import -> recognize & review -> offline export.  Provider setup is modal and
optional.  All potentially billable network calls require fresh confirmation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

from ai_bridge import AIConfig, load_profile, save_profile, store_key, forget_key, probe_provider
from deepseek_workflow import recognize, write_review_bundle, manifest_from_ocr
from engine import convert
from ui_flow import StudioState, local_ocr_manifest, RASTER

FONT=('Microsoft YaHei UI',10)
BG='#f4f7fb'; WHITE='#ffffff'; INK='#17253a'; SUB='#64748b'; BLUE='#265ad6'; LINE='#dbe4ef'; LIGHT='#e9efff'


def provider_config(data, key='') -> AIConfig:
    """Build a provider configuration without persisting the secret."""
    return AIConfig(base_url=data['base_url'].strip(),
                    vision_model=data['vision_model'].strip(),
                    edit_model=data.get('edit_model','gpt-image-2.5-flare').strip(),
                    key_env=data.get('key_env','CELL_SU7_API_KEY').strip(),
                    api_key=key,timeout=180).validate()


def _safe_open(path:Path):
    """Open only a locally generated file/folder; no shell string interpolation."""
    if sys.platform=='win32':os.startfile(str(path))
    elif sys.platform=='darwin':subprocess.Popen(['open',str(path)])
    else:subprocess.Popen(['xdg-open',str(path)])


def _font(size=14):
    candidates=['C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/arial.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/System/Library/Fonts/Supplemental/Arial.ttf']
    for p in candidates:
        try:return ImageFont.truetype(p,size)
        except OSError:pass
    return ImageFont.load_default()




class ScrollablePage(tk.Frame):
    """A genuine, two-axis scrolling viewport for a complete wizard step.

    A page's controls are children of ``content``. Content can grow in height;
    under Windows display scaling it may also need to grow in width. Both
    scrollbars track its real requested size rather than cutting off widgets.
    """

    def __init__(self, master, *, bg=WHITE):
        super().__init__(master, bg=bg)
        self.canvas=tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, takefocus=0)
        self.vertical=ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.horizontal=ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.vertical.grid(row=0, column=1, sticky='ns')
        self.horizontal.grid(row=1, column=0, sticky='ew')
        self.horizontal.grid_remove()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.content=tk.Frame(self.canvas, bg=bg)
        self._window=self.canvas.create_window((0,0), window=self.content, anchor='nw')
        self._pending=False
        self.content.bind('<Configure>', self._request_remeasure, add='+')
        self.canvas.bind('<Configure>', self._request_remeasure, add='+')

    def _request_remeasure(self, event=None):
        if not self._pending:
            self._pending=True
            self.after_idle(self._measure)

    def _measure(self):
        self._pending=False
        if not self.winfo_exists():return
        viewport_w=max(1,self.canvas.winfo_width())
        natural_w=self.content.winfo_reqwidth()
        target_w=max(viewport_w,natural_w)
        self.canvas.itemconfigure(self._window, width=target_w)
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        if natural_w>viewport_w+3:
            self.horizontal.grid()
        else:
            self.horizontal.grid_remove()
            self.canvas.xview_moveto(0)
        # The horizontal bar changes the vertical viewport height.
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def scroll(self, steps, *, horizontal=False):
        if horizontal:self.canvas.xview_scroll(int(steps),'units')
        else:self.canvas.yview_scroll(int(steps),'units')

    def reset_scroll(self):
        self.canvas.yview_moveto(0)
        self.canvas.xview_moveto(0)


class StudioUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('CELL SU7 Studio  •  图片重建工作台')
        screen_w,screen_h=self.winfo_screenwidth(),self.winfo_screenheight()
        self.geometry(f'{min(1180,max(540,screen_w-90))}x{min(810,max(390,screen_h-110))}')
        self.minsize(540,380)
        self.configure(bg=BG)
        self.state=StudioState()
        self.stage=0
        self.busy=False
        self.use_manifest=True
        self.mode=tk.StringVar(value='none')
        self.show_extra_modes=tk.BooleanVar(value=False)
        self.tiling=tk.StringVar(value='single')
        self.purpose=tk.StringVar(value='text_recognition')
        self.export_svg=tk.BooleanVar(value=True)
        self.export_pdf=tk.BooleanVar(value=False)
        self.quality=tk.BooleanVar(value=False)
        self.clean=tk.BooleanVar(value=False)
        self.backend=tk.StringVar(value='auto')
        self.colors=tk.IntVar(value=32)
        self.color_mode=tk.StringVar(value='perceptual')
        self.structure_mode=tk.StringVar(value='objects')
        self.separate_regions=tk.BooleanVar(value=False)
        self.status=tk.StringVar(value='选择一张图片开始。默认不会上传文件。')
        self.source_text=tk.StringVar(value='尚未选择图片')
        self.output_text=tk.StringVar(value='')
        profile=load_profile()
        self.provider=tk.StringVar(value='deepseek' if 'deepseek.com' in profile.get('base_url','') else 'custom')
        self.base_url=tk.StringVar(value=profile.get('base_url','https://api.deepseek.com'))
        self.vision_model=tk.StringVar(value=profile.get('vision_model','deepseek-flash'))
        self.edit_model=tk.StringVar(value=profile.get('edit_model',AIConfig.edit_model))
        self.key_env=tk.StringVar(value=profile.get('key_env',AIConfig.key_env))
        self.api_key=tk.StringVar(value='')
        self._source_cache=None
        self._preview_shapes=[]
        self._modal_scrollers={}
        self._visual_plan=None
        self._setup_style();self._layout();self._show_step(0)
        # Cross-platform scrolling, including wheel events over child labels/canvases.
        self.bind_all('<MouseWheel>',self._wheel,add='+')
        self.bind_all('<Button-4>',self._wheel,add='+')
        self.bind_all('<Button-5>',self._wheel,add='+')
        self.bind('<Control-Home>',lambda e:self._keyboard_scroll(0),add='+')
        self.bind('<Control-End>',lambda e:self._keyboard_scroll(1),add='+')

    def _setup_style(self):
        s=ttk.Style(self)
        if 'clam' in s.theme_names():s.theme_use('clam')
        s.configure('TFrame',background=BG)
        s.configure('Card.TFrame',background=WHITE)
        s.configure('TLabel',background=BG,foreground=INK,font=FONT)
        s.configure('Card.TLabel',background=WHITE,foreground=INK,font=FONT)
        s.configure('Sub.TLabel',background=WHITE,foreground=SUB,font=FONT)
        s.configure('H1.TLabel',background=WHITE,foreground=INK,font=('Microsoft YaHei UI',21,'bold'))
        s.configure('H2.TLabel',background=WHITE,foreground=INK,font=('Microsoft YaHei UI',13,'bold'))
        s.configure('Nav.TButton',padding=(14,15),font=('Microsoft YaHei UI',10),anchor='w')
        s.configure('TButton',padding=(13,8),font=FONT)
        s.configure('Accent.TButton',background=BLUE,foreground='white',padding=(18,10),font=('Microsoft YaHei UI',10,'bold'))
        s.map('Accent.TButton',background=[('active','#1f4bb8'),('disabled','#a9b9e6')])
        s.configure('Card.TRadiobutton',background=WHITE,foreground=INK,font=FONT,padding=(3,8))
        s.configure('Card.TCheckbutton',background=WHITE,foreground=INK,font=FONT,padding=(3,4))
        s.configure('Treeview',background=WHITE,fieldbackground=WHITE,foreground=INK,rowheight=28,font=FONT)
        s.configure('Treeview.Heading',font=('Microsoft YaHei UI',10,'bold'),padding=6)
        s.configure('TEntry',padding=7)
        s.configure('Status.Horizontal.TProgressbar',background=BLUE)

    def _layout(self):
        # A horizontal stepper replaces the fixed-width sidebar, giving narrow
        # laptop / HiDPI windows more room for the actual work.
        header=ttk.Frame(self,padding=(18,10));header.pack(fill='x')
        title_row=ttk.Frame(header);title_row.pack(fill='x')
        self.title_row=title_row
        ttk.Label(title_row,text='CELL SU7  /  STUDIO',font=('Microsoft YaHei UI',15,'bold')).pack(side='left')
        self.header_controls=ttk.Frame(header)
        ttk.Button(self.header_controls,text='AI 设置',command=self._provider_dialog).pack(side='right',padx=(6,0))
        ttk.Button(self.header_controls,text='帮助',command=self._help).pack(side='right')
        self.header_controls.pack(in_=title_row,side='right')
        self._compact_header=False
        self.subtitle=ttk.Label(header,text='选图 → 文字校对（可跳过）→ 导出可编辑 PPTX / SVG',foreground=SUB)
        self.subtitle.pack(anchor='w',fill='x',pady=(3,0))
        nav=ttk.Frame(self,padding=(18,4));nav.pack(fill='x')
        self.nav=[]
        for i,caption in enumerate(('① 导入图片','② 校对文字','③ 导出结果')):
            btn=ttk.Button(nav,text=caption,style='Nav.TButton',command=lambda s=i:self._show_step(s))
            btn.pack(side='left',fill='x',expand=True,padx=(0,6) if i<2 else (0,0))
            self.nav.append(btn)
        ttk.Separator(self).pack(fill='x',pady=(5,0))
        self.main=tk.Frame(self,bg=WHITE,highlightbackground=LINE,highlightthickness=1)
        self.main.pack(fill='both',expand=True,padx=13,pady=(9,0))
        self.pages=[]
        self.screens=[]
        for _ in range(3):
            page=ScrollablePage(self.main)
            page.place(relx=0,rely=0,relwidth=1,relheight=1)
            self.pages.append(page)
            self.screens.append(page.content)
        self._build_import(self.screens[0]);self._build_review(self.screens[1]);self._build_export(self.screens[2])
        bottom=ttk.Frame(self,padding=(15,8));bottom.pack(fill='x')
        self.progress=ttk.Progressbar(bottom,mode='indeterminate',length=95,style='Status.Horizontal.TProgressbar')
        self.progress.pack(side='right')
        self.status_label=ttk.Label(bottom,textvariable=self.status,foreground=SUB)
        self.status_label.pack(anchor='w',fill='x')
        self.bind('<Configure>',self._responsive_labels,add='+')

    def _responsive_labels(self,event):
        if event.widget is not self:return
        width=max(220,event.width-135)
        self.status_label.configure(wraplength=width)
        self.subtitle.configure(wraplength=max(200,event.width-50))
        compact=event.width<660
        if compact!=self._compact_header:
            self._compact_header=compact
            self.header_controls.pack_forget()
            if compact:self.header_controls.pack(side='top',anchor='e',pady=(4,0),before=self.subtitle)
            else:self.header_controls.pack(in_=self.title_row,side='right')

    def _wheel(self,event):
        """Scroll the visible step or a modal, without stealing Treeview scrolling."""
        try:
            widget=event.widget
            top=widget.winfo_toplevel()
            if top is self:
                page=self.pages[self.stage]
            else:
                page=self._modal_scrollers.get(top)
                if page is None:return
            # Text editors and drop-downs retain their native wheel handling.
            # When a Treeview reaches its end, wheel events continue to the page.
            cls=widget.winfo_class()
            if cls in ('TCombobox','Listbox','Text','Scrollbar','TScrollbar'):
                return
            if getattr(event,'num',None)==4:steps=-3
            elif getattr(event,'num',None)==5:steps=3
            else:
                delta=getattr(event,'delta',0)
                if not delta:return
                steps=(-1 if delta>0 else 1)*max(1,round(abs(delta)/120))*3
            if cls=='Treeview':
                start,end=widget.yview()
                if not ((steps>0 and end>=0.999) or (steps<0 and start<=0.001)):
                    return
            page.scroll(steps,horizontal=bool(getattr(event,'state',0)&0x1))
            return 'break'
        except tk.TclError:
            return

    def _keyboard_scroll(self,at_bottom):
        page=self.pages[self.stage]
        page.canvas.yview_moveto(float(at_bottom))
        return 'break'

    def _register_modal_scroller(self,dialog):
        page=ScrollablePage(dialog,bg=WHITE)
        page.pack(fill='both',expand=True)
        self._modal_scrollers[dialog]=page
        def cleanup(event):
            if event.widget is dialog:self._modal_scrollers.pop(dialog,None)
        dialog.bind('<Destroy>',cleanup,add='+')
        return page.content

    def _dialog_size(self,dialog,desired_w,desired_h):
        w=min(desired_w,max(420,self.winfo_screenwidth()-95))
        h=min(desired_h,max(300,self.winfo_screenheight()-120))
        dialog.geometry(f'{w}x{h}')
        dialog.minsize(400,300)

    def _text(self,parent,title,subtitle):
        ttk.Label(parent,text=title,style='H1.TLabel').pack(anchor='w',pady=(0,4))
        ttk.Label(parent,text=subtitle,style='Sub.TLabel',wraplength=840).pack(anchor='w',pady=(0,16))

    def _wrap(self,parent,**kwargs):
        f=ttk.Frame(parent,style='Card.TFrame',**kwargs)
        return f

    def _build_import(self,root):
        box=self._wrap(root,padding=(18,16));box.pack(fill='both',expand=True)
        self._text(box,'导入原始图片','第一步只需选图，再选是否识别文字。高级参数和 API 设置可稍后调整。')
        row=self._wrap(box);row.pack(fill='x')
        ttk.Button(row,text='选择图片…',style='Accent.TButton',command=self._choose_source).pack(side='left')
        ttk.Label(row,textvariable=self.source_text,style='Sub.TLabel',wraplength=650).pack(side='left',padx=(15,0),fill='x',expand=True)
        ttk.Label(box,text='原图预览',style='H2.TLabel').pack(anchor='w',pady=(18,8))
        preview=tk.Frame(box,bg='#edf2f9',height=250,highlightbackground=LINE,highlightthickness=1)
        preview.pack(fill='both',expand=True)
        preview.pack_propagate(False)
        self.import_preview=tk.Canvas(preview,bg='#edf2f9',highlightthickness=0)
        self.import_preview.pack(fill='both',expand=True)
        self.import_preview.bind('<Configure>',lambda e:self._show_preview(self.import_preview,with_boxes=False))
        ttk.Label(box,text='如何处理文字？',style='H2.TLabel').pack(anchor='w',pady=(16,4))
        for value,text in [('none','不识别文字 · 只转换图形（最快、完全离线）'),
                           ('local','本地 OCR · 免费，无需联网（需要 Tesseract）'),
                           ('deepseek','DeepSeek OCR · 云端识别与坐标（可能计费）')]:
            ttk.Radiobutton(box,text=text,value=value,variable=self.mode,style='Card.TRadiobutton').pack(anchor='w',pady=(1,0))
        ttk.Checkbutton(box,text='显示其他 AI 模式（实验功能）',style='Card.TCheckbutton',
                        variable=self.show_extra_modes,command=self._toggle_extra_modes).pack(anchor='w')
        self.extra_modes=self._wrap(box)
        for value,caption in [('vision','其他视觉模型 OCR · 需要支持图片输入和 JSON 输出'),
                              ('layout','AI 语义图形重建 · 适合简单流程图，可能收费')]:
            ttk.Radiobutton(self.extra_modes,text=caption,value=value,variable=self.mode,style='Card.TRadiobutton').pack(anchor='w')
        end=self._wrap(box);end.pack(fill='x',pady=(6,0))
        self.next_btn=ttk.Button(end,text='直接进入离线导出 →',style='Accent.TButton',command=self._next_from_import)
        self.next_btn.pack(side='right')
        self.mode.trace_add('write',lambda *_:self._mode_updated())

    def _build_review(self,root):
        box=self._wrap(root,padding=(18,16));box.pack(fill='both',expand=True)
        self._text(box,'识别并校对文字','不再编辑 JSON：直接双击表格修改识别内容与文字框。未确认前不会擦除原图文字。')
        actions=self._wrap(box);actions.pack(fill='x',pady=(0,10))
        self.scan_btn=ttk.Button(actions,text='开始识别文字',style='Accent.TButton',command=self._scan)
        self.scan_btn.pack(side='left',padx=(0,8))
        ttk.Button(actions,text='导入已有 JSON',command=self._import_manifest).pack(side='left',padx=(0,8))
        self.tile_combo=ttk.Combobox(actions,textvariable=self.tiling,values=['single','2x2'],state='readonly',width=8)
        self.tile_combo.pack(side='right')
        self.tile_label=ttk.Label(actions,text='识别区域',style='Sub.TLabel')
        self.tile_label.pack(side='right',padx=(0,6))
        self.review_hint=tk.StringVar(value='暂无识别结果。也可以跳过文字，直接导出图形。')
        ttk.Label(box,textvariable=self.review_hint,style='Sub.TLabel').pack(anchor='w',pady=(0,10))
        panel=self._wrap(box);panel.pack(fill='both',expand=True)
        self.review_panel=panel
        preview_side=self._wrap(panel);preview_side.pack(side='left',fill='both',expand=True,padx=(0,12))
        self.preview_side=preview_side
        ttk.Label(preview_side,text='原图与文字框',style='H2.TLabel').pack(anchor='w',pady=(0,6))
        self.review_preview=tk.Canvas(preview_side,bg='#edf2f9',height=320,highlightthickness=1,highlightbackground=LINE)
        self.review_preview.pack(fill='both',expand=True)
        self.review_preview.bind('<Configure>',lambda e:self._show_preview(self.review_preview,with_boxes=True))
        self.review_preview.bind('<Button-1>',self._preview_clicked)
        table_side=self._wrap(panel);table_side.pack(side='right',fill='both',expand=True)
        self.table_side=table_side
        self._review_orientation='side'
        ttk.Label(table_side,text='可编辑文字清单',style='H2.TLabel').pack(anchor='w',pady=(0,6))
        cols=('id','content','bbox')
        self.table=ttk.Treeview(table_side,columns=cols,show='headings',height=9,selectmode='browse')
        for col,heading,width in [('id','#',38),('content','文字内容',170),('bbox','位置',130)]:
            self.table.heading(col,text=heading)
            self.table.column(col,width=width,minwidth=35,stretch=col!='id')
        scroll=ttk.Scrollbar(table_side,orient='vertical',command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        self.table.bind('<Double-1>',lambda e:self._edit_selected())
        self.table.bind('<<TreeviewSelect>>',lambda e:self._show_preview(self.review_preview,with_boxes=True))
        tools=self._wrap(box);tools.pack(fill='x',pady=(12,0))
        ttk.Button(tools,text='＋ 添加文字',command=lambda:self._edit_item(None)).pack(side='left',padx=(0,5))
        ttk.Button(tools,text='修改选中',command=self._edit_selected).pack(side='left',padx=(0,5))
        ttk.Button(tools,text='删除选中',command=self._delete_selected).pack(side='left',padx=(0,5))
        ttk.Button(tools,text='保存 JSON',command=self._save_review).pack(side='left',padx=(5,0))
        ttk.Button(tools,text='🎯 画布拖动文字 / 图形',command=self._open_visual_editor).pack(side='left',padx=(9,0))
        nextbar=self._wrap(box);nextbar.pack(fill='x',pady=(16,0))
        ttk.Button(nextbar,text='跳过文字 → 导出',command=self._skip_text).pack(side='left')
        self.approve_btn=ttk.Button(nextbar,text='确认校对完成 → 导出',style='Accent.TButton',command=self._approve)
        self.approve_btn.pack(side='right')
        # Collapse the image/table columns into a vertical stack when the
        # viewport is narrow; every action remains accessible by scrolling.
        self.pages[1].canvas.bind('<Configure>',self._review_responsive,add='+')

    def _review_responsive(self,event):
        want='stack' if event.width<790 else 'side'
        if want==self._review_orientation:return
        self._review_orientation=want
        self.preview_side.pack_forget()
        self.table_side.pack_forget()
        if want=='stack':
            self.review_preview.configure(height=265)
            self.table.configure(height=8)
            self.preview_side.pack(side='top',fill='x',padx=0,pady=(0,14))
            self.table_side.pack(side='top',fill='x')
        else:
            self.review_preview.configure(height=320)
            self.table.configure(height=9)
            self.preview_side.pack(side='left',fill='both',expand=True,padx=(0,12))
            self.table_side.pack(side='right',fill='both',expand=True)
        self.pages[1]._request_remeasure()

    def _build_export(self,root):
        box=self._wrap(root,padding=(18,16));box.pack(fill='both',expand=True)
        self.export_intro=tk.StringVar(value='只在本地生成文件，不会再次调用 DeepSeek 或扣费。')
        ttk.Label(box,text='导出可编辑文件',style='H1.TLabel').pack(anchor='w',pady=(0,4))
        ttk.Label(box,textvariable=self.export_intro,style='Sub.TLabel',wraplength=840).pack(anchor='w',pady=(0,16))
        ttk.Label(box,text='导出 PowerPoint 文件',style='H2.TLabel').pack(anchor='w',pady=(7,6))
        row=self._wrap(box);row.pack(fill='x')
        ttk.Entry(row,textvariable=self.output_text).pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='选择位置…',command=self._choose_output).pack(side='left',padx=(8,0))
        ttk.Label(box,text='附加输出',style='H2.TLabel').pack(anchor='w',pady=(22,5))
        ttk.Checkbutton(box,text='同时导出可编辑 SVG',style='Card.TCheckbutton',variable=self.export_svg).pack(anchor='w')
        ttk.Checkbutton(box,text='同时导出 PDF（需要 CairoSVG）',style='Card.TCheckbutton',variable=self.export_pdf).pack(anchor='w')
        ttk.Checkbutton(box,text='生成图像质量对照 HTML（需要 CairoSVG）',style='Card.TCheckbutton',variable=self.quality).pack(anchor='w')
        ttk.Label(box,text='文字处理',style='H2.TLabel').pack(anchor='w',pady=(20,5))
        self.export_review_info=tk.StringVar(value='当前未导入文字清单；只生成图形对象')
        ttk.Label(box,textvariable=self.export_review_info,style='Sub.TLabel',wraplength=800).pack(anchor='w',pady=(0,5))
        self.clean_cb=ttk.Checkbutton(box,text='根据已审核文字框局部去字（复杂图形可能受影响，默认关闭）',
                                     style='Card.TCheckbutton',variable=self.clean)
        self.clean_cb.pack(anchor='w')
        advanced=self._wrap(box);advanced.pack(fill='x',pady=(16,4))
        self.advanced_visible=tk.BooleanVar(value=False)
        ttk.Checkbutton(advanced,text='显示高级矢量化选项',style='Card.TCheckbutton',variable=self.advanced_visible,
                        command=self._toggle_advanced).pack(anchor='w')
        self.advanced=self._wrap(box)
        controls_one=self._wrap(self.advanced)
        controls_one.pack(fill='x',anchor='w',pady=(3,4))
        ttk.Label(controls_one,text='矢量后端',style='Card.TLabel').pack(side='left')
        ttk.Combobox(controls_one,textvariable=self.backend,
                     values=['auto','precision','contour','vtracer'],
                     state='readonly',width=12).pack(side='left',padx=(8,16))
        ttk.Label(controls_one,text='配色策略',style='Card.TLabel').pack(side='left')
        ttk.Combobox(controls_one,textvariable=self.color_mode,
                     values=['perceptual','balanced','fusion','legacy'],
                     state='readonly',width=12).pack(side='left',padx=(8,8))
        controls_two=self._wrap(self.advanced)
        controls_two.pack(fill='x',anchor='w',pady=(3,4))
        ttk.Label(controls_two,text='颜色分层数',style='Card.TLabel').pack(side='left')
        ttk.Spinbox(controls_two,from_=2,to=64,textvariable=self.colors,width=6).pack(side='left',padx=(8,16))
        ttk.Label(controls_two,text='结构识别',style='Card.TLabel').pack(side='left')
        ttk.Combobox(controls_two,textvariable=self.structure_mode,
                     values=['hybrid','annotate','off'],
                     state='readonly',width=11).pack(side='left',padx=(8,8))
        ttk.Checkbutton(self.advanced,text='拆分较大的同色区域，便于单独移动编辑',
                        style='Card.TCheckbutton',variable=self.separate_regions).pack(anchor='w',pady=(3,0))
        self.export_summary=tk.StringVar(value='')
        self.export_summary_label=ttk.Label(box,textvariable=self.export_summary,style='Sub.TLabel',wraplength=800)
        self.export_summary_label.pack(anchor='w',pady=(18,7))
        end=self._wrap(box);end.pack(fill='x',pady=(8,0))
        self.export_btn=ttk.Button(end,text='开始生成 PPTX →',style='Accent.TButton',command=self._export)
        self.export_btn.pack(side='left')
        ttk.Button(end,text='打开导出目录',command=self._open_output_folder).pack(side='left',padx=(11,0))
        ttk.Button(end,text='可视化编辑 / 对比',command=self._open_visual_editor).pack(side='left',padx=(11,0))
        ttk.Label(box,text='提示：默认使用免费的高保真像素边界矢量化。可编辑色块与原始语义对象不同，复杂图仍需核对。',
                  style='Sub.TLabel',wraplength=780).pack(anchor='w',pady=(20,0))

    def _mode_updated(self):
        self._update_next_caption()
        if hasattr(self,'scan_btn'):
            modes={'none':'当前无需识别','local':'开始本地 OCR','deepseek':'开始 DeepSeek OCR',
                   'vision':'开始视觉模型 OCR','layout':'AI 结构图直接在导出阶段处理'}
            self.scan_btn.configure(text=modes.get(self.mode.get(),'开始识别文字'))
        if hasattr(self,'tile_combo') and hasattr(self,'tile_label'):
            if self.mode.get()=='deepseek':
                self.tile_combo.pack(side='right');self.tile_label.pack(side='right',padx=(0,6))
            else:
                self.tile_combo.pack_forget();self.tile_label.pack_forget()
        if hasattr(self,'export_intro'):
            self._refresh_export()

    def _toggle_extra_modes(self):
        if self.show_extra_modes.get():self.extra_modes.pack(fill='x')
        else:
            if self.mode.get() in ('vision','layout'):self.mode.set('none')
            self.extra_modes.pack_forget()

    def _update_next_caption(self):
        self.next_btn.configure(text='直接进入导出 →' if self.mode.get() in ('none','layout') else '下一步：识别与校对 →')

    def _next_from_import(self):
        if self.mode.get() in ('none','layout'):self._skip_text()
        else:self._show_step(1)

    def _toggle_advanced(self):
        if self.advanced_visible.get():self.advanced.pack(fill='x',pady=(5,5),before=self.export_summary_label)
        else:self.advanced.pack_forget()

    def _show_step(self,idx):
        if self.busy:return
        if idx>0 and not self.state.has_image:
            messagebox.showwarning('还没有原图','先在第 1 步选择图片。');return
        changed=idx!=self.stage
        self.stage=idx;self.pages[idx].tkraise()
        if changed:self.pages[idx].reset_scroll()
        for i,btn in enumerate(self.nav):
            btn.configure(text=('● ' if i==idx else '')+['① 导入图片','② 校对文字','③ 导出结果'][i])
        if idx==1:
            self._mode_updated()
            self._refresh_review()
        if idx==2:
            self._refresh_export()
        self.status.set(['选择原始图片，再决定是否识别文字。','文字校对不用修改 JSON 文件；双击表格即可编辑。','离线导出，不会调用付费 API。'][idx])

    def _choose_source(self):
        if self.busy:return
        value=filedialog.askopenfilename(title='选择科研原图',filetypes=[('图片与 SVG','*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.svg')])
        if not value:return
        if self.state.dirty and not messagebox.askyesno('更换图片？','当前文字清单尚未保存；更换图片会放弃未保存的修改。继续吗？'):return
        try:
            self.state.open_source(value)
        except Exception as e:messagebox.showerror('导入失败',str(e));return
        self._source_cache=None
        self._visual_plan=None
        self.source_text.set(str(self.state.source));self.output_text.set(str(self.state.output))
        self.clean.set(False);self.use_manifest=True;self._refresh_review();self._refresh_export()
        self._show_preview(self.import_preview,False)
        self.status.set(f'已导入：{self.state.source.name}（{self.state.image_size[0]}×{self.state.image_size[1]}）')

    def _source_image(self):
        if not self.state.source:return None
        if self._source_cache is not None:return self._source_cache
        if self.state.source.suffix.lower()=='.svg':
            try:
                import cairosvg
                from io import BytesIO
                # File is user's local input; CairoSVG may require local dependencies.
                raw=cairosvg.svg2png(url=str(self.state.source),output_width=min(1500,self.state.image_size[0]))
                self._source_cache=Image.open(BytesIO(raw)).convert('RGB')
                return self._source_cache
            except Exception:return None
        with Image.open(self.state.source) as im:
            self._source_cache=ImageOps.exif_transpose(im).convert('RGB')
            return self._source_cache

    def _show_preview(self,canvas,with_boxes):
        if not canvas.winfo_exists():return
        canvas.delete('all')
        w=max(120,canvas.winfo_width());h=max(100,canvas.winfo_height())
        if not self.state.has_image:
            canvas.create_text(w/2,h/2,text='请先选择一张科研图片',fill=SUB,font=FONT)
            return
        try:im=self._source_image()
        except Exception:im=None
        if im is None:
            canvas.create_text(w/2,h/2,text='SVG 文件已导入（此环境无法预览 SVG）',fill=SUB,font=FONT)
            return
        sw,sh=im.size;scale=min((w-22)/sw,(h-22)/sh,1.8)
        target=(max(1,round(sw*scale)),max(1,round(sh*scale)))
        image=im.resize(target,Image.Resampling.LANCZOS)
        canvas._preview_photo=ImageTk.PhotoImage(image)
        xx=(w-target[0])/2;yy=(h-target[1])/2
        canvas.create_image(xx,yy,image=canvas._preview_photo,anchor='nw')
        if not with_boxes:return
        self._preview_shapes=[]
        selected=self._selected_id()
        for item in (self.state.manifest or {}).get('texts',[]):
            if 'bbox' not in item:continue
            x0,y0,x1,y1=item['bbox'];ident=int(item.get('id',0))
            a=xx+x0*scale;b=yy+y0*scale;c=xx+x1*scale;d=yy+y1*scale
            chosen=ident==selected
            color='#215edb' if chosen else ('#18a56a' if self.state.approved else '#e35b49')
            canvas.create_rectangle(a,b,c,d,outline=color,width=3 if chosen else 2)
            canvas.create_text(a,max(10,b-8),text=str(ident),fill=color,anchor='w',font=('Arial',10,'bold'))
            self._preview_shapes.append((ident,(a,b,c,d)))

    def _preview_clicked(self,event):
        for ident,(x0,y0,x1,y1) in reversed(self._preview_shapes):
            if x0-2<=event.x<=x1+2 and y0-2<=event.y<=y1+2:
                iid=str(ident)
                if self.table.exists(iid):
                    self.table.selection_set(iid);self.table.see(iid)
                return

    def _refresh_review(self):
        for row in self.table.get_children():self.table.delete(row)
        manifest=self.state.manifest or {}
        for item in manifest.get('texts',[]):
            box=item.get('bbox',[])
            ident=int(item['id']);desc=','.join(str(int(x)) for x in box)
            self.table.insert('', 'end',iid=str(ident),values=(ident,item['contents'],desc))
        if not self.state.has_image:
            hint='请先选择原图。'
        elif self.state.has_texts:
            hint=f'识别/编辑文字：{self.state.review_count} 条；'+('已确认校对，可离线导出' if self.state.approved else '尚待确认校对')
        else:
            hint='暂无文字清单。可点击“开始识别文字”，也可以跳过直接导出图形。'
        self.review_hint.set(hint)
        self._show_preview(self.review_preview,True)
        self._refresh_export()

    def _selected_id(self):
        selection=self.table.selection()
        return int(selection[0]) if selection else None

    def _scan(self):
        if self.busy or not self.state.source:return
        if self.state.source.suffix.lower() not in RASTER:
            messagebox.showinfo('SVG 不需要 OCR','SVG 原有文字可以直接保留编辑；请进入导出步骤。');return
        mode=self.mode.get()
        if mode=='none':
            messagebox.showinfo('未启用 OCR','你在第一步选择了“不识别文字”。可以跳过文字校对直接导出；如需 OCR，请返回第一步选择本地或 DeepSeek。')
            return
        if self.state.dirty and not messagebox.askyesno('重新识别？','已有尚未保存的文字修改；重新识别会覆盖它们。继续吗？'):return
        config=None
        if mode in ('deepseek','vision'):
            try:config=self._get_config()
            except Exception as e:messagebox.showerror('API 配置错误',str(e));return
            if mode=='deepseek' and self.base_url.get().rstrip('/').replace('/v1','')!='https://api.deepseek.com':
                messagebox.showwarning('先设置官方接口','你选择了 DeepSeek OCR，但当前 API 地址不是 DeepSeek 官方地址。请点右上角“AI 接口设置”，选 DeepSeek 官方服务。');return
            if not config.key():
                messagebox.showwarning('缺少密钥','请在右上角“AI 接口设置”填写 DeepSeek API Key，或使用环境变量。');return
            calls=4 if mode=='deepseek' and self.tiling.get()=='2x2' else 1
            if not messagebox.askyesno('确认可能收费的 OCR 请求',
                f'即将向 {self.base_url.get()} 上传图片 {self.state.source.name}。\n'
                f'请求次数：{calls} 次（2x2 会增加费用）\n'
                '网络超时后服务器仍可能完成计费，请勿盲目重试。\n\n是否确认发送？'):
                return
        source=self.state.source
        tile=self.tiling.get(); purpose=self.purpose.get()
        self._busy(True, '正在识别文字。请勿重复提交可能收费的请求…' if mode=='deepseek' else '本地 OCR 正在识别文字…')
        def worker():
            try:
                img=self._source_image()
                if mode in ('deepseek','vision'):
                    import numpy as np
                    if mode=='deepseek':
                        recognized=recognize(np.asarray(img),config,tiling=tile,purpose=purpose)
                    else:
                        from ai_bridge import analyze_image
                        recognized=analyze_image(np.asarray(img),config,'ocr')
                        recognized.update(tile_mode='single',request_count=1,page_count=1,
                                          warnings=['视觉模型文字框属于估计值，请人工校对'])
                    dest=self.state.review_path().parent
                    paths=write_review_bundle(np.asarray(img),recognized,dest)
                    doc=json.loads(Path(paths['manifest']).read_text(encoding='utf-8'))
                    note='；'.join(recognized.get('warnings',[])[:3])
                    partial=bool(recognized.get('partial'))
                    manifest_path=paths['manifest']
                else:
                    import pytesseract
                    data=pytesseract.image_to_data(img,lang='eng',output_type=pytesseract.Output.DICT,config='--psm 11')
                    doc=local_ocr_manifest(img,data);manifest_path=None;note='本地 OCR 结果必须人工审核';partial=False
                def done():
                    self.state.set_manifest(doc,manifest_path)
                    self.use_manifest=True
                    self._refresh_review()
                    self.status.set(f'识别完成：{self.state.review_count} 条文字。{note}')
                    if partial:
                        messagebox.showwarning('DeepSeek OCR 输出不完整',
                           f'模型输出达到长度上限；已安全保留 {self.state.review_count} 个完整文字框。\n'
                           '请在画布检查漏字和位置。需要补识别时，可切换 2x2 分区后自行再次点击“识别”，\n'
                           '程序不会自动重新提交，以免产生额外费用。')
                    if not self.state.has_texts:messagebox.showwarning('识别结果为空','没有获得带有效位置的文字，请直接导出图形或尝试手动添加文字。')
                self.after(0,done)
            except Exception as e:
                error=str(e)
                self.after(0,lambda:messagebox.showerror('OCR 识别失败',error))
                self.after(0,lambda:self.status.set('OCR 未完成：'+error))
            finally:self.after(0,lambda:self._busy(False))
        threading.Thread(target=worker,daemon=True).start()

    def _import_manifest(self):
        if not self.state.has_image:messagebox.showwarning('请先选图','导入文字清单前请选择对应的原图');return
        path=filedialog.askopenfilename(title='选择已经识别或审核的文字 JSON',filetypes=[('JSON','*.json')])
        if not path:return
        if self.state.dirty and not messagebox.askyesno('替换文字清单','当前修改尚未保存，确认替换吗？'):return
        try:self.state.load_manifest(path)
        except Exception as e:messagebox.showerror('清单无效',str(e));return
        self.use_manifest=True;self._refresh_review();self.status.set('已导入文字清单；请确认文字后再导出。')

    def _edit_selected(self):
        ident=self._selected_id()
        if ident is None:
            messagebox.showinfo('请选择文字','在表格中选中一行后再修改。');return
        self._edit_item(ident)

    def _edit_item(self,ident):
        if not self.state.has_image:return
        item=None
        if ident is not None:
            item=next((x for x in (self.state.manifest or {}).get('texts',[]) if int(x.get('id',-1))==ident),None)
        win=tk.Toplevel(self);win.title('修改文字与位置' if item else '添加可编辑文字')
        win.transient(self);self._dialog_size(win,600,430);win.grab_set()
        win.configure(bg=WHITE)
        content=self._register_modal_scroller(win)
        pad=ttk.Frame(content,style='Card.TFrame',padding=18);pad.pack(fill='both',expand=True)
        entry=tk.StringVar(value=item.get('contents','') if item else '')
        dims=self.state.image_size
        box=item.get('bbox',[round(dims[0]*.3),round(dims[1]*.3),round(dims[0]*.5),round(dims[1]*.36)]) if item else [round(dims[0]*.3),round(dims[1]*.3),round(dims[0]*.5),round(dims[1]*.36)]
        coords=[tk.StringVar(value=str(round(v,1))) for v in box]
        size=tk.StringVar(value=str(item.get('fontSize',round((box[3]-box[1])*1.05,1))) if item else str(round((box[3]-box[1])*1.05,1)))
        ttk.Label(pad,text='文字内容',style='H2.TLabel').pack(anchor='w',pady=(0,5))
        t=ttk.Entry(pad,textvariable=entry,width=50);t.pack(fill='x');t.focus_set()
        ttk.Label(pad,text=f'坐标（以原图 {dims[0]}×{dims[1]} 像素为准）',style='Card.TLabel').pack(anchor='w',pady=(17,6))
        cr=ttk.Frame(pad,style='Card.TFrame');cr.pack(fill='x')
        for label,var in zip(('左 X','上 Y','右 X','下 Y'),coords):
            cell=ttk.Frame(cr,style='Card.TFrame');cell.pack(side='left',padx=(0,9))
            ttk.Label(cell,text=label,style='Sub.TLabel').pack(anchor='w')
            ttk.Entry(cell,textvariable=var,width=9).pack(pady=(4,0))
        ttk.Label(pad,text='字号（像素）',style='Card.TLabel').pack(anchor='w',pady=(17,5))
        ttk.Entry(pad,textvariable=size,width=12).pack(anchor='w')
        def save():
            try:
                modified=self.state.change_text(ident,entry.get(),[float(c.get()) for c in coords],float(size.get()))
                self._refresh_review()
                self.table.selection_set(str(modified))
                win.destroy()
            except Exception as e:messagebox.showerror('内容不合法',str(e),parent=win)
        bottom=ttk.Frame(pad,style='Card.TFrame');bottom.pack(fill='x',pady=(22,0))
        ttk.Button(bottom,text='取消',command=win.destroy).pack(side='right',padx=(7,0))
        ttk.Button(bottom,text='保存修改',style='Accent.TButton',command=save).pack(side='right')
        win.bind('<Return>',lambda e:save())

    def _delete_selected(self):
        ident=self._selected_id()
        if ident is None:return
        if not messagebox.askyesno('删除文字',f'删除第 {ident} 条文字框？这不会修改原图。'):return
        self.state.remove_text(ident);self._refresh_review()

    def _save_review(self):
        if not self.state.manifest:messagebox.showinfo('暂无清单','请先识别、导入或手动添加文字。');return
        try:
            path=self.state.manifest_path or self.state.review_path()
            actual=self.state.save_manifest(path)
            self.status.set(f'文字清单已保存：{actual}')
        except Exception as e:messagebox.showerror('保存失败',str(e))

    def _approve(self):
        if not self.state.manifest:
            messagebox.showinfo('没有文字清单','可以使用“跳过文字 → 导出”只重建图形。');return
        try:
            self.state.approve()
            self.state.save_manifest(self.state.manifest_path or self.state.review_path())
        except Exception as e:messagebox.showerror('审核失败',str(e));return
        self.clean.set(False)
        self.use_manifest=True
        self._show_step(2)
        self.status.set(f'已确认 {self.state.review_count} 条文字；接下来离线生成 PPTX。')

    def _skip_text(self):
        self.clean.set(False)
        self.use_manifest=False
        # Do not silently use unapproved OCR.  Skipping disables the manifest for export.
        self._show_step(2)
        self.status.set('已进入离线导出。未审核的文字清单不会默认插入。')

    def _refresh_export(self):
        has_text=self.state.has_texts
        if self.mode.get()=='layout':
            self.export_intro.set('高级 AI 图形重建：将上传图片调用视觉模型，需确认计费。不是高保真无损转换。')
        else:
            self.export_intro.set('此步骤完全离线，不会再次调用 API 或产生 OCR 费用。')
        if has_text and self.state.approved and self.use_manifest:
            label=f'已审核 {self.state.review_count} 条文字：将恢复为可编辑文本。'
            self.clean_cb.state(['!disabled'])
        elif has_text:
            label=(f'识别到 {self.state.review_count} 条文字，但当前选择跳过；不会在导出时插入。'
                   if not self.use_manifest and self.state.approved else
                   f'识别到 {self.state.review_count} 条文字，但尚未确认；导出时不会自动使用。')
            self.clean.set(False);self.clean_cb.state(['disabled'])
        else:
            label='暂无可用文字清单：本次只重建图形（原图中的文字会作为图形痕迹留在结果中）。'
            self.clean.set(False);self.clean_cb.state(['disabled'])
        self.export_review_info.set(label)
        if self.mode.get()=='layout':self.clean.set(False);self.clean_cb.state(['disabled'])
        if self.state.output and not self.output_text.get():self.output_text.set(str(self.state.output))

    def _choose_output(self):
        if not self.state.source:return
        current=self.output_text.get() or str(self.state.output)
        value=filedialog.asksaveasfilename(title='导出 PowerPoint',initialfile=Path(current).name,
            initialdir=str(Path(current).parent),defaultextension='.pptx',filetypes=[('PowerPoint','*.pptx')])
        if value:self.output_text.set(value)

    def _open_visual_editor(self):
        """Open real scene preview; offline only, never asks the provider."""
        if self.busy or not self.state.has_image:return
        if self.mode.get()=='layout':
            messagebox.showinfo('请先切换本地模式','语义重建会调用 AI。画布编辑目前基于本地重建，可先切换为纯本地/DeepSeek OCR 模式。')
            return
        try:
            from visual_editor import EditableDocument, VisualEditor, source_signature
            from engine import create_scene
            import tempfile
            self.state.output=Path(self.output_text.get()).expanduser().resolve() if self.output_text.get() else self.state.output
            settings=self.state.offline_export_options(
                clean=bool(self.clean.get() and self.state.approved and self.use_manifest),
                backend=self.backend.get(),colors=self.colors.get(),
                color_mode=self.color_mode.get(),structure_mode=self.structure_mode.get(),
                precision_separate_regions=self.separate_regions.get())
            original=self._source_image()
            if original is None:raise ValueError('无法预览该 SVG；请安装 CairoSVG')
            identity=source_signature(self.state.source,settings)
        except Exception as exc:
            messagebox.showerror('画布初始化失败',str(exc));return
        self._busy(True,'正在本地准备矢量画布…此操作不会上传图片或扣费。')
        doc=self.state.manifest if (self.state.has_texts and self.use_manifest) else None
        src=self.state.source
        existing=self._visual_plan
        def worker():
            try:
                with tempfile.TemporaryDirectory(prefix='su7-edit-') as root:
                    man=None
                    if doc:
                        man=Path(root)/'current-text.json'
                        man.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8')
                    scene,_=create_scene(src,settings,str(man) if man else None)
                # Reusing a previous plan is deliberate: reject mismatch loudly.
                editor=EditableDocument(scene,identity,doc,existing)
                def show():
                    self._busy(False)
                    VisualEditor(self,original,editor,self._visual_saved)
                self.after(0,show)
            except Exception as exc:
                err=str(exc)
                self.after(0,lambda:self._busy(False))
                self.after(0,lambda:messagebox.showerror('画布生成失败',err))
        threading.Thread(target=worker,daemon=True).start()

    def _visual_saved(self,editor):
        from copy import deepcopy
        plan=editor.plan()
        # Change the studio's real manifest, never merely shift an overlay.
        for ident in editor.changed_text_ids:
            item=next(x for x in editor.manifest['texts'] if int(x['id'])==ident)
            self.state.change_text(ident,item['contents'],item['bbox'],item['fontSize'])
        self._visual_plan=deepcopy(plan)
        dest=self.state.review_path().with_name('visual-edits.json')
        dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        self._refresh_review();self._refresh_export()
        updated=(len(plan.get('shape_moves',[]))+len(plan.get('native_text_moves',[]))+len(plan.get('ocr_text_ids',[])))
        self.status.set(f'已保存画布编辑：{updated} 个矢量位移；文字框已同步。图形位移将在导出 PPTX/SVG 时真实应用。')
        if editor.changed_text_ids:
            messagebox.showinfo('需要确认文字','拖动文字框后已更新清单；请在“校对文字”页确认并审核，再导出。')

    def _export(self):
        if self.busy or not self.state.has_image:return
        try:
            target=Path(self.output_text.get()).expanduser().resolve()
            if target.suffix.lower()!='.pptx':raise ValueError('请把导出文件名设为 .pptx')
            if target==self.state.source:raise ValueError('禁止覆盖原图')
            if target.exists() and not messagebox.askyesno('覆盖已有文件？',f'{target.name} 已存在，是否覆盖？'):return
            self.state.output=target
            do_clean=self.clean.get() and self.use_manifest
            mode=self.mode.get()
            if self._visual_plan and mode!='layout':
                from visual_editor import source_signature
                if self._visual_plan['identity']!=source_signature(self.state.source,
                      self.state.offline_export_options(clean=do_clean,
                      backend=self.backend.get(),colors=self.colors.get(),
                color_mode=self.color_mode.get(),structure_mode=self.structure_mode.get(),
                precision_separate_regions=self.separate_regions.get())):
                    raise ValueError('画布编辑后修改了后端、颜色数或去字参数。为防止移动错误对象，请重新打开可视化画布后再导出。')
            if mode=='layout':
                if self._visual_plan:raise ValueError('已存在本地画布编辑，不能直接套用到 AI 语义重建。请切换本地模式或更换图片。')
                config=self._get_config()
                if not config.key():raise ValueError('请在右上角设置视觉模型 API Key')
                if not messagebox.askyesno('确认可能收费的视觉调用',
                    f'将发送图片到 {config.base_url}，调用模型 {config.vision_model}。\n此步骤不是离线操作，可能产生费用。\n确认发送吗？'):
                    return
                settings=self.state.offline_export_options(clean=False,
                    backend=self.backend.get(),colors=self.colors.get(),
                color_mode=self.color_mode.get(),structure_mode=self.structure_mode.get(),
                precision_separate_regions=self.separate_regions.get())
                settings.ai_mode='layout';settings.ai_config=config
            else:
                settings=self.state.offline_export_options(clean=do_clean,
                    backend=self.backend.get(),colors=self.colors.get(),
                color_mode=self.color_mode.get(),structure_mode=self.structure_mode.get(),
                precision_separate_regions=self.separate_regions.get())
            man=None
            if self.state.has_texts and self.state.approved and self.use_manifest:
                path=target.with_suffix('.reviewed.json')
                self.state.save_manifest(path)
                man=str(path)
            svg=str(target.with_suffix('.svg')) if self.export_svg.get() else None
            pdf=str(target.with_suffix('.pdf')) if self.export_pdf.get() else None
            report=str(target.with_suffix('.quality.html')) if self.quality.get() else None
            graph=str(target.with_suffix('.structure.json')) if mode!='layout' and self.backend.get() in ('auto','precision') else None
            dests=[str(target)]+[x for x in (svg,pdf,report,graph) if x]
            for other in dests:
                if Path(other).resolve()==self.state.source:raise ValueError('输出不能覆盖输入文件')
            existing=[Path(x).name for x in dests[1:] if Path(x).exists()]
            if existing and not messagebox.askyesno('覆盖额外结果？','下列文件会被覆盖：\n'+'\n'.join(existing)+'\n确认继续吗？'):return
        except Exception as e:messagebox.showerror('检查导出参数失败',str(e));return
        self._busy(True,'正在调用视觉模型（可能产生费用）…' if mode=='layout' else '正在本地生成 PPTX。不会发起任何 AI 网络请求…')
        source=str(self.state.source)
        def worker():
            try:
                if self._visual_plan:
                    result=convert(source,str(target),settings,man,svg,pdf,None,report,
                                   edit_plan=self._visual_plan,scene_graph_out=graph)
                else:
                    result=convert(source,str(target),settings,man,svg,pdf,None,report,scene_graph_out=graph)
                self.after(0,lambda:self._export_done(result,target))
            except Exception as e:
                error=str(e)
                self.after(0,lambda:messagebox.showerror('导出失败',error))
                self.after(0,lambda:self.status.set('导出失败：'+error))
            finally:self.after(0,lambda:self._busy(False))
        threading.Thread(target=worker,daemon=True).start()

    def _export_done(self,result,target):
        self.state.last_result=result
        summary=f'完成：{result["shapes"]} 个图形、{result["texts"]} 条可编辑文本。\n文件：{target}'
        if target.with_suffix('.structure.json').exists():summary+='\n结构候选：'+str(target.with_suffix('.structure.json'))
        if result.get('warnings'):
            summary+='\n注意：'+str(result['warnings'][0])
        self.export_summary.set(summary)
        self.status.set('✓ PPTX 导出成功，可以在 PowerPoint 打开并编辑。')
        messagebox.showinfo('导出完成',summary)

    def _open_output_folder(self):
        path=Path(self.output_text.get()).expanduser().parent
        if not path.exists():messagebox.showinfo('找不到目录','导出目录不存在');return
        try:_safe_open(path)
        except Exception as e:messagebox.showerror('无法打开目录',str(e))

    def _busy(self,on,message=''):
        self.busy=bool(on)
        if on:self.progress.start(12)
        else:self.progress.stop()
        for btn in (self.scan_btn,self.export_btn,self.approve_btn):
            btn.state(['disabled'] if on else ['!disabled'])
        if message:self.status.set(message)

    def _get_config(self):
        data={'base_url':self.base_url.get(),'vision_model':self.vision_model.get(),
              'edit_model':self.edit_model.get(),'key_env':self.key_env.get()}
        return provider_config(data,self.api_key.get())

    def _provider_dialog(self):
        if self.busy:return
        dialog=tk.Toplevel(self);dialog.title('AI 接口设置 · 默认仅用于 OCR')
        self._dialog_size(dialog,620,660)
        dialog.transient(self);dialog.grab_set()
        content=self._register_modal_scroller(dialog)
        panel=ttk.Frame(content,style='Card.TFrame',padding=18);panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='模型服务商',style='H2.TLabel').pack(anchor='w',pady=(0,5))
        choice=ttk.Combobox(panel,values=['DeepSeek 官方','其他 OpenAI 兼容服务'],state='readonly')
        choice.set('DeepSeek 官方' if 'deepseek.com' in self.base_url.get() else '其他 OpenAI 兼容服务')
        choice.pack(fill='x')
        def change_provider(event=None):
            # Never forward a pasted key to a newly selected origin by accident.
            self.api_key.set('')
            if choice.get()=='DeepSeek 官方':
                self.base_url.set('https://api.deepseek.com');self.vision_model.set('deepseek-flash')
        choice.bind('<<ComboboxSelected>>',change_provider)
        for title,var in [('API Base URL',self.base_url),('视觉模型（自定义兼容服务使用）',self.vision_model),
                          ('图片编辑模型（高级用途）',self.edit_model),('API Key 环境变量名称',self.key_env)]:
            ttk.Label(panel,text=title,style='Card.TLabel').pack(anchor='w',pady=(12,4))
            ttk.Entry(panel,textvariable=var).pack(fill='x')
        ttk.Label(panel,text='API Key（只在本次程序运行中保存，除非选择系统凭据库）',style='Card.TLabel').pack(anchor='w',pady=(13,4))
        ttk.Entry(panel,textvariable=self.api_key,show='●').pack(fill='x')
        tip=tk.StringVar(value='连通性测试不上传图片，也不执行 OCR。')
        ttk.Label(panel,textvariable=tip,style='Sub.TLabel',wraplength=540).pack(anchor='w',pady=(14,6))
        buttons=ttk.Frame(panel,style='Card.TFrame');buttons.pack(fill='x',pady=(13,0))
        def show_error(title,exc):messagebox.showerror(title,str(exc),parent=dialog)
        def save():
            try:save_profile(self._get_config());tip.set('已保存接口设置（不含 API Key）。')
            except Exception as exc:show_error('保存失败',exc)
        def credential():
            try:
                store_key(self._get_config());self.api_key.set('');tip.set('密钥已保存至系统凭据库。')
            except Exception as exc:show_error('保存失败',exc)
        def forget():
            try:forget_key(self._get_config());tip.set('系统保存的密钥已删除。')
            except Exception as exc:show_error('删除失败',exc)
        def test():
            try:cfg=self._get_config()
            except Exception as exc:show_error('配置错误',exc);return
            tip.set('正在测试 /models 连接（无图片上传）…')
            def worker():
                try:msg=probe_provider(cfg)
                except Exception as exc:msg=str(exc)
                self.after(0,lambda:tip.set(msg))
            threading.Thread(target=worker,daemon=True).start()
        ttk.Button(buttons,text='测试连接（免费）',command=test).pack(side='left',padx=(0,8))
        ttk.Button(buttons,text='保存配置',command=save).pack(side='left',padx=(0,8))
        ttk.Button(buttons,text='存入系统凭据库',command=credential).pack(side='left')
        def diagnose():
            try:
                from ai_bridge import diagnose_deepseek_connection
                cfg=self._get_config()
            except Exception as exc:show_error('无法诊断',exc);return
            if 'deepseek.com' not in cfg.base_url:
                tip.set('当前不是 DeepSeek 地址；请先选择 DeepSeek 官方服务。');return
            tip.set('正在检查 DNS、HTTPS 与代理配置（不会上传图片）…')
            def worker():
                try:
                    info=diagnose_deepseek_connection(cfg,check_auth=True)
                    result='DNS：'+info['dns']+'\nHTTPS：'+info['https']+'\n代理：'+('有' if info['uses_proxy'] else '无')+'\n\n'+info['advice']
                except Exception as exc:result=str(exc)
                def show():
                    tip.set('DeepSeek 网络诊断完成。')
                    if dialog.winfo_exists():messagebox.showinfo('DeepSeek 连接诊断（未上传图片）',result,parent=dialog)
                self.after(0,show)
            threading.Thread(target=worker,daemon=True).start()
        ttk.Button(panel,text='诊断 DeepSeek 网络（免费）',command=diagnose).pack(anchor='w',pady=(11,0))
        ttk.Button(panel,text='清除已存密钥',command=forget).pack(anchor='w',pady=(8,0))
        ttk.Button(panel,text='关闭',command=dialog.destroy).pack(anchor='e',pady=(8,0))

    def _help(self):
        messagebox.showinfo('三步使用说明',
           '① 选择图片及文字识别方式。\n'
           '② 需要 OCR 时，直接识别并双击表格修改；审核结果会保存为 JSON，之后无需重新调用 API。\n'
           '③ 选择导出位置，生成 PPTX / SVG。\n'
           '可在校对或导出页点击“可视化编辑”，鼠标拖动文字和矢量对象；支持对照、缩放、撤销。\n\n'
           '如果不需要 OCR，可直接进入导出。\n'
           '缩小窗口或提高系统缩放后，可使用页面滚动条、鼠标滚轮或触控板访问所有选项。\n'
           '导出始终离线，复杂科研图仍需要人工校验。')


def launch():
    StudioUI().mainloop()
