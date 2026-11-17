"""Regression tests for the missing controls after Windows DPI/window resize."""
import os
from pathlib import Path
import pytest

pytestmark=pytest.mark.skipif(
    os.environ.get('CELL_SU7_GUI_TESTS')!='1',
    reason='Run with CELL_SU7_GUI_TESTS=1 xvfb-run -a python -m pytest',
)


def _flush(win):
    win.update()
    win.update_idletasks()
    win.update()


def test_all_three_steps_scroll_to_bottom_on_compact_window():
    from studio_ui import StudioUI
    w=StudioUI()
    try:
        w.geometry('650x430')
        w.tk.call('tk','scaling',2.0)
        w.state.open_source(Path(__file__).resolve().parents[1]/'examples'/'demo_scientific.png')
        for i,button in enumerate((w.next_btn,w.approve_btn,w.export_btn)):
            w._show_step(i)
            _flush(w)
            page=w.pages[i]
            assert page.content.winfo_reqheight()>page.canvas.winfo_height()
            first=page.canvas.yview()[0]
            page.canvas.yview_moveto(1)
            _flush(w)
            assert page.canvas.yview()[0]>first
            # Primary button can be reached in each step, even after scrolling.
            assert button.winfo_ismapped()
            top=page.canvas.winfo_rooty()
            y=button.winfo_rooty()
            assert top<=y<top+page.canvas.winfo_height()
    finally:w.destroy()


def test_mouse_wheel_and_compact_review_stack():
    from studio_ui import StudioUI
    from types import SimpleNamespace
    w=StudioUI()
    try:
        w.geometry('600x450')
        w.state.open_source(Path(__file__).resolve().parents[1]/'examples'/'demo_scientific.png')
        w._show_step(1)
        _flush(w)
        assert w._review_orientation=='stack'
        assert w.table_side.winfo_manager()=='pack'
        pane=w.pages[1]
        before=pane.canvas.yview()[0]
        event=SimpleNamespace(widget=w.review_preview,delta=-120,num=0,state=0)
        assert w._wheel(event)=='break'
        _flush(w)
        assert pane.canvas.yview()[0]>before
        w.geometry('1150x760')
        _flush(w)
        assert w._review_orientation=='side'
    finally:w.destroy()


def test_narrow_window_horizontal_scroll_and_responsive_header():
    from studio_ui import StudioUI
    w=StudioUI()
    try:
        w.geometry('540x390')
        w.tk.call('tk','scaling',2.2)
        _flush(w)
        assert w._compact_header
        w.state.open_source(Path(__file__).resolve().parents[1]/'examples'/'demo_scientific.png')
        w._show_step(1)
        _flush(w)
        page=w.pages[1]
        # Narrow, high-DPI review actions intentionally require horizontal fallback.
        assert page.content.winfo_reqwidth()>page.canvas.winfo_width()+3
        assert page.horizontal.winfo_ismapped()
        page.canvas.xview_moveto(1)
        assert page.canvas.xview()[0]>0
        assert w.header_controls.winfo_ismapped()
    finally:w.destroy()


def test_ai_settings_scroller_reaches_footer_on_small_screen():
    from studio_ui import StudioUI
    from tkinter import ttk
    w=StudioUI()
    try:
        w.geometry('650x430')
        w._provider_dialog()
        _flush(w)
        assert len(w._modal_scrollers)==1
        dialog,page=next(iter(w._modal_scrollers.items()))
        dialog.geometry('450x330')
        _flush(w)
        assert page.content.winfo_reqheight()>page.canvas.winfo_height()
        assert page.canvas.yview()[1]<1
        page.canvas.yview_moveto(1)
        _flush(w)
        assert page.canvas.yview()[1]>=0.999
        close_buttons=[x for x in page.content.winfo_children() if isinstance(x,ttk.Frame)]
        assert close_buttons
        dialog.destroy()
        _flush(w)
        assert not w._modal_scrollers
    finally:w.destroy()
