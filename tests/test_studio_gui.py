import os
import pytest
from pathlib import Path

@pytest.mark.skipif(os.environ.get('CELL_SU7_GUI_TESTS')!='1',reason='Run with CELL_SU7_GUI_TESTS=1 xvfb-run -a pytest')
def test_gui_three_steps_and_skipping_does_not_export_unreviewed_ocr():
    from studio_ui import StudioUI
    w=StudioUI()
    try:
        w.update()
        w.state.open_source(Path(__file__).resolve().parents[1]/'examples'/'demo_scientific.png')
        w.source_text.set(str(w.state.source));w.output_text.set(str(w.state.output))
        w.state.set_manifest({'source_canvas':{'width':1080,'height':589},'review_required':True,'texts':[
            {'id':1,'contents':'NLRP3','bbox':[7,7,70,35],'position':[7,35],'fontSize':24,'reviewed':False}]})
        w._show_step(1);w.update()
        assert w.stage==1 and w.table.exists('1')
        w._skip_text();w.update()
        assert w.stage==2 and not w.use_manifest
        w._refresh_export();assert '不会' in w.export_review_info.get()
        w._show_step(1);w.state.approve();w.use_manifest=True;w._show_step(2)
        w.update()
        assert w.clean_cb.instate(['!disabled'])
        w.advanced_visible.set(True);w._toggle_advanced();w.update()
        assert w.advanced.winfo_manager()=='pack'
        # This is an interface-only smoke test: no network and no file export.
    finally:w.destroy()

@pytest.mark.skipif(os.environ.get('CELL_SU7_GUI_TESTS')!='1',reason='Run with CELL_SU7_GUI_TESTS=1 xvfb-run -a pytest')
def test_gui_offline_export_does_not_request_ai(tmp_path,monkeypatch):
    from PIL import Image
    import studio_ui
    output=tmp_path/'result.pptx'
    image=tmp_path/'sample.png';Image.new('RGB',(160,90),'white').save(image)
    captured=[]
    def fake_convert(src,dest,settings,manifest,svg,pdf,manifest_out,report,**kwargs):
        assert settings.ai_mode=='off' and not settings.ocr and not settings.ai_remote_clean
        assert src==str(image) and dest==str(output)
        captured.append(settings)
        output.write_bytes(b'test-only')
        return {'shapes':3,'texts':0,'warnings':[]}
    monkeypatch.setattr(studio_ui,'convert',fake_convert)
    monkeypatch.setattr(studio_ui.messagebox,'showinfo',lambda *a,**kw:None)
    monkeypatch.setattr(studio_ui.messagebox,'showerror',lambda *a,**kw:pytest.fail(str(a)))
    w=studio_ui.StudioUI()
    try:
        w.state.open_source(image);w.output_text.set(str(output))
        w.export_svg.set(False);w.export_pdf.set(False);w.quality.set(False)
        w._show_step(2);w.update()
        w._export()
        w.after(3000,w.quit)
        w.mainloop()
        assert captured and w.state.last_result['shapes']==3
    finally:w.destroy()
