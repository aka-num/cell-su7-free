"""Runs with CELL_SU7_GUI_TESTS=1 xvfb-run -a python -m pytest."""
import os,sys,time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

@pytest.mark.skipif(os.environ.get('CELL_SU7_GUI_TESTS')!='1',reason='needs GUI display')
def test_visual_canvas_drag_roundtrip(tmp_path):
    import tkinter as tk
    from PIL import Image
    from studio_ui import StudioUI
    from visual_editor import VisualEditor,EditableDocument,source_signature
    from engine import Settings, Scene, Shape, Text
    from geometry import polygon,path_bbox
    image=tmp_path/'img.png';Image.new('RGB',(300,200),'white').save(image)
    source=Image.open(image)
    scene=Scene(300,200,[Shape(polygon([(35,50),(125,50),(125,95),(35,95)]),fill=(20,120,210))],
                [Text('NLRP3',135,74,18)])
    parent=StudioUI();parent.withdraw()
    results=[]
    editor=VisualEditor(parent,source,EditableDocument(scene,source_signature(image,Settings())),results.append)
    try:
        editor.update()
        deadline=time.monotonic()+8
        while editor.rebuilt is None and time.monotonic()<deadline:
            editor.update();time.sleep(.07)
        assert editor.rebuilt is not None, editor.message.get()
        c=editor.views[1];editor.filter.set('shape')
        a,b,d,e=editor._screen_rect(c,(35,50,125,95))
        x,y=round((a+d)/2),round((b+e)/2)
        from types import SimpleNamespace
        editor._press(SimpleNamespace(x=x,y=y,state=0))
        editor._drag(SimpleNamespace(x=x+25,y=y+11,state=0))
        editor._release(SimpleNamespace(x=x+25,y=y+11,state=0))
        editor.update()
        assert editor.doc.history
        bbox=path_bbox(editor.doc.scene.shapes[0].path)
        assert bbox[0]>35 and bbox[1]>50
        editor._save()
        assert len(results)==1 and len(results[0].plan()['shape_moves'])==1
    finally:
        if editor.winfo_exists():editor.destroy()
        parent.destroy()

@pytest.mark.skipif(os.environ.get('CELL_SU7_GUI_TESTS')!='1',reason='needs GUI display')
def test_studio_can_open_save_canvas_then_offline_export(tmp_path,monkeypatch):
    from tkinter import messagebox
    from studio_ui import StudioUI
    from visual_editor import VisualEditor
    from pptx import Presentation
    svg=tmp_path/'diagram.svg'
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="220" height="130">'
                   '<rect x="15" y="20" width="55" height="35" fill="blue"/>'
                   '</svg>')
    pptx=tmp_path/'edited.pptx'
    monkeypatch.setattr(messagebox,'showinfo',lambda *a,**kw:None)
    monkeypatch.setattr(messagebox,'showwarning',lambda *a,**kw:None)
    monkeypatch.setattr(messagebox,'showerror',lambda *a,**kw:pytest.fail(str(a)))
    studio=StudioUI();studio.geometry('940x640+10+10')
    studio.state.open_source(svg);studio.output_text.set(str(pptx))
    studio.export_svg.set(False);studio.export_pdf.set(False);studio.quality.set(False)
    studio._show_step(2);studio._open_visual_editor()
    found=[]
    def await_dialog():
        children=[c for c in studio.winfo_children() if isinstance(c,VisualEditor)]
        if children and children[0].rebuilt is not None:
            found.append(children[0]);studio.quit()
        else:studio.after(70,await_dialog)
    studio.after(80,await_dialog)
    studio.after(12000,studio.quit)
    try:
        studio.mainloop()
        assert found,'Studio did not launch a usable local editor'
        dialog=found[0]
        dialog.doc.move('shape',0,20,10)
        dialog._save()
        assert studio._visual_plan and studio._visual_plan['shape_moves']
        studio._export()
        def check_done():
            if studio.state.last_result is not None:studio.quit()
            else:studio.after(80,check_done)
        studio.after(100,check_done)
        studio.after(12000,studio.quit)
        studio.mainloop()
        assert pptx.exists()
        shapes=Presentation(pptx).slides[0].shapes
        assert len(shapes)>=1
        # SVG paths and their altered coordinates are written as OOXML elements.
        assert studio.state.last_result['visual_edits']==1
    finally:
        studio.destroy()
