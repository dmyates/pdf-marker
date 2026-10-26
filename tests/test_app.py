"""GTK interaction and shared text-layout regression tests; run under Xvfb."""
import time
import ctypes
import ctypes.util
import os
import pytest
import pymupdf as pdf
from pdfmarker.app import Window
from pdfmarker.gtk import Gtk, Gdk, GLib, Pango, Graphene
from pdfmarker.text import laid_out, text_layout
from pdfmarker.document import Mark


def pump():
    end = time.monotonic()+.08
    while time.monotonic()<end:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        time.sleep(.002)


@pytest.fixture(scope='module')
def app():
    Gtk.init()
    app = Gtk.Application(application_id='io.github.pdfmarker.Tests')
    app.register(None)
    return app


@pytest.fixture
def window(app,sample):
    w = Window(app)
    errors = []
    w.error = lambda title,error:errors.append((title,str(error)))
    w.present()
    pump()
    w.load(sample)
    pump()
    w.zoom.set_selected(4)
    pump()
    yield w
    w.canvas.finish_edit(cancel=True)
    w._closing = True
    w.close()
    pump()
    assert not errors


def point(w,p,x,y):
    ox,oy = w.canvas.origins[p]
    return (ox+x)*w.canvas.zoom,(oy+y)*w.canvas.zoom


def click(w,p,x,y,**kwargs):
    w.canvas.press(*point(w,p,x,y),**kwargs)
    w.canvas.release(*point(w,p,x,y))
    pump()


def drag(w,p,start,end):
    w.canvas.press(*point(w,p,*start))
    w.canvas.move(*point(w,p,*end))
    w.canvas.release(*point(w,p,*end))
    pump()


def test_inline_lifecycle_and_focused_export(window):
    w = window
    w.canvas.set_tool('text')
    click(w,0,80,250)
    assert isinstance(w.canvas.editor,Gtk.TextView)
    w.canvas.editor.get_buffer().set_text('Text from UI')
    assert w.canvas.pending and not w.document.marks
    assert w.export()
    assert w.canvas.editor is None
    assert w.document.marks[0].appearance
    with pdf.open(w.last_export) as saved:
        assert 'Text from UI' in saved[0].get_text()
    w.canvas.begin_text(w.document.marks[0],0)
    w.canvas.editor.get_buffer().set_text('Edited inline')
    w.canvas.editor_key(None,Gdk.KEY_Return,0,Gdk.ModifierType.CONTROL_MASK)
    assert w.document.marks[0].text=='Edited inline'
    w.history(False)
    assert w.document.marks[0].text=='Text from UI'
    w.history(True)
    assert w.document.marks[0].text=='Edited inline'


def test_cancel_blank_delete_and_editor_undo(window):
    w=window
    w.canvas.set_tool('text')
    click(w,0,80,250)
    w.canvas.editor.get_buffer().set_text('Cancel this')
    w.canvas.editor_key(None,Gdk.KEY_Escape,0,0)
    assert not w.document.dirty
    click(w,0,80,250)
    assert w.canvas.finish_edit()
    assert not w.document.can_undo
    click(w,0,80,250)
    buffer=w.canvas.editor.get_buffer()
    buffer.insert_at_cursor('First',-1)
    w.history(False)
    assert w.canvas.editor_text()==''
    w.history(True)
    assert w.canvas.editor_text()=='First'
    w.canvas.delete_selected()
    assert not w.document.marks
    w.history(False)
    assert w.document.marks[0].text=='First'


def test_tools_and_fields(window):
    w=window
    click(w,0,50,150)
    assert w.document.values['agree']=='Yes'
    for tool,x in [('tick',350),('cross',400)]:
        w.canvas.set_tool(tool)
        click(w,0,x,150)
    w.canvas.set_tool('draw')
    drag(w,0,(80,300),(180,350))
    assert {m.kind for m in w.document.marks}=={'tick','cross','draw'}
    assert w.export()


def test_move_all_marks_and_group_history(window):
    w=window
    marks=[laid_out(Mark('text',0,(80,230,280,270),text='Move me')),
           Mark('tick',0,(80,300,100,320)),Mark('cross',0,(180,300,200,320)),
           Mark('draw',0,(80,380,180,420),points=((80,420),(130,380),(180,410)))]
    for m in marks:
        w.document.add(m)
    w.canvas.set_tool('select')
    for i,m in enumerate(marks):
        x,y=m.rect[0]+5,m.rect[1]+5
        drag(w,0,(x,y),(x+20,y+20))
        assert w.document.marks[i].rect[:2]==(m.rect[0]+20,m.rect[1]+20)
    w.canvas.selected={0,1,2,3}
    before=list(w.document.marks)
    drag(w,0,(105,255),(115,265))
    assert [m.rect[0] for m in w.document.marks]==[m.rect[0]+10 for m in before]
    w.canvas.delete_selected()
    assert not w.document.marks
    w.history(False)
    assert len(w.document.marks)==4
    w.history(False)
    assert w.document.marks==before


def test_resize_and_group_settings(window):
    w=window
    m=laid_out(Mark('text',0,(60,240,300,310),text='A line of text that should wrap when the box becomes narrower.'))
    w.document.add(m)
    w.canvas.set_tool('select')
    click(w,0,70,250)
    drag(w,0,(300,310),(170,320))
    assert w.document.marks[0].rect[2]==170
    assert text_layout(w.document.marks[0]).get_line_count()>1
    w.size_control.set_value(18)
    assert w.document.marks[0].size==18
    w.stroke_control.set_value(3)
    assert w.document.marks[0].width==3
    rgba=Gdk.RGBA();rgba.parse('#3366cc');w.color.set_rgba(rgba)
    assert abs(w.document.marks[0].color[2]-.8)<.001
    w.document.add(Mark('tick',0,(300,300,320,320)))
    w.canvas.selected={0,1}
    w.canvas.style_selected(color=(.2,.3,.4))
    assert all(m.color==(.2,.3,.4) for m in w.document.marks)
    w.history(False)
    assert w.document.marks[1].color==(0,0,0)


@pytest.mark.parametrize('zoom',[.75,1,2])
def test_display_editor_and_pdf_lines_baselines_color_unicode(window,zoom):
    w=window
    text='Zoë signs this form. A second sentence wraps across several lines. Ελληνικά'
    m=laid_out(Mark('text',0,(70,250,230,390),text=text,size=15,color=(.1,.2,.7)))
    w.document.add(m)
    w.canvas.set_zoom(zoom)
    w.canvas.begin_text(m,0)
    pump()
    layout=text_layout(m)
    iterator=layout.get_iter()
    expected=[]
    while True:
        line=iterator.get_line_readonly()
        expected.append((text.encode()[line.start_index:line.start_index+line.length].decode().strip(),iterator.get_baseline()/Pango.SCALE))
        if not iterator.next_line():break
    # TextView remains allocated in document units at every zoom.
    assert w.canvas.editor.get_width()==160
    buf=w.canvas.editor.get_buffer()
    native=[]
    it=buf.get_start_iter()
    while True:
        start=it.copy()
        end=it.copy()
        w.canvas.editor.forward_display_line_end(end)
        rect=w.canvas.editor.get_iter_location(start)
        native.append((buf.get_text(start,end,True).strip(),rect.y))
        if not w.canvas.editor.forward_display_line(it):break
    assert [t for t,y in native]==[t for t,y in expected]
    with pdf.open(stream=w.document.pdf_bytes(),filetype='pdf') as result:
        lines=[line for b in result[0].get_text('dict',clip=pdf.Rect(m.rect))['blocks'] for line in b.get('lines',[])]
        assert [''.join(s['text'] for s in line['spans']).strip() for line in lines]==[t for t,y in expected]
        for line,(_,baseline),(_,native_y) in zip(lines,expected,native):
            assert abs(line['spans'][0]['origin'][1]-(250+baseline))<.2
            assert abs(native_y-(baseline-layout.get_context().get_metrics(layout.get_font_description(),None).get_ascent()/Pango.SCALE))<1.1
            assert line['spans'][0]['color']==0x1933b2 or abs(line['spans'][0]['color']-0x1933b2)<=0x010101
    w.canvas.finish_edit(cancel=True)


def test_continuous_scroll_page_tracking_and_bounded_cache(window):
    w=window
    assert w.canvas.origins[1][1]>w.canvas.origins[0][1]+650
    adj=w.scroll.get_vadjustment()
    adj.set_value(700)
    pump()
    assert w.canvas.page==1
    assert w.page_number.get_value_as_int()==2
    assert w.pages.get_selected_row().get_index()==1
    assert len(w.canvas.cache)<=2
    w.canvas.set_tool('text')
    click(w,1,80,250)
    w.canvas.editor.get_buffer().set_text('On page two')
    assert w.export()
    with pdf.open(w.last_export) as result:
        assert 'On page two' not in result[0].get_text()
        assert 'On page two' in result[1].get_text()
    w.canvas.scroll_to_page(0)
    pump()
    assert w.canvas.page==0 and 0 in w.canvas.cache


def test_cross_page_move(window):
    w=window
    w.document.add(Mark('cross',0,(80,300,100,320)))
    w.canvas.set_tool('select')
    w.canvas.press(*point(w,0,85,305))
    end=point(w,1,105,255)
    w.canvas.move(*end)
    assert w.canvas.preview[0].page==1
    w.canvas.release(*end)
    assert w.document.marks[0].page==1
    assert w.document.marks[0].rect==(100,250,120,270)
    w.history(False)
    assert w.document.marks[0].page==0


def test_help_and_canvas_shortcuts_do_not_affect_editor(window):
    w=window
    w.canvas.key(None,Gdk.KEY_question,0,0)
    assert w.help.get_visible()
    w.toggle_help()
    for key,tool in [('v','select'),('f','fill'),('t','text'),('d','draw'),('k','tick'),('x','cross')]:
        assert w.canvas.key(None,Gdk.keyval_from_name(key),0,0)
        assert w.canvas.tool==tool
    w.canvas.begin_text(Mark('text',0,(40,250,240,300)))
    for key in ('v','f','t','d','k','x','question'):
        assert not w.canvas.editor_key(None,Gdk.keyval_from_name(key),0,0)


def test_pending_close_can_cancel_and_replacement_can_discard(window,sample):
    w=window
    w.canvas.begin_text(Mark('text',0,(40,250,240,300)))
    w.canvas.editor.get_buffer().set_text('Unsaved')
    continued=[]
    w.confirm_discard(lambda:continued.append(True))
    pump()
    dialog=next(x for x in w.get_application().get_windows() if isinstance(x,Gtk.Dialog))
    assert w.document.marks[0].text=='Unsaved'
    dialog.response(Gtk.ResponseType.CANCEL)
    assert not continued
    old=w.document
    w.load(sample)
    pump()
    dialog=next(x for x in w.get_application().get_windows() if isinstance(x,Gtk.Dialog))
    dialog.response(Gtk.ResponseType.REJECT)
    assert w.document is not old


def test_form_dialogs_use_canonical_values(window):
    w=window
    field=next(f for f in w.document.fields if f.name=='choice')
    w.fill_field(field)
    pump()
    dialog=next(x for x in w.get_application().get_windows() if isinstance(x,Gtk.Dialog))
    editor=dialog.get_content_area().get_first_child().get_last_child()
    editor.set_selected(1)
    dialog.response(Gtk.ResponseType.OK)
    assert w.document.values['choice']=='Two'
    field=next(f for f in w.document.fields if f.name=='name')
    w.fill_field(field)
    pump()
    dialog=next(x for x in w.get_application().get_windows() if isinstance(x,Gtk.Dialog))
    dialog.response(Gtk.ResponseType.REJECT)
    assert w.document.values['name']==''


def test_empty_window(app):
    w=Window(app)
    assert not w.actions['export'].get_enabled()
    assert w.empty.get_visible()
    assert not w.export()
    w._closing=True
    w.close()


class NativeInput:
    """Deliver real X11 events, including GTK controller dispatch and focus."""
    def __init__(self,w):
        import gi
        gi.require_version('GdkX11','4.0')
        from gi.repository import GdkX11
        self.w=w
        self.x=ctypes.CDLL(ctypes.util.find_library('X11'))
        self.xt=ctypes.CDLL(ctypes.util.find_library('Xtst'))
        self.x.XOpenDisplay.restype=ctypes.c_void_p
        self.d=self.x.XOpenDisplay(None)
        self.x.XKeysymToKeycode.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        self.x.XKeysymToKeycode.restype=ctypes.c_uint
        self.x.XFlush.argtypes=[ctypes.c_void_p]
        self.x.XCloseDisplay.argtypes=[ctypes.c_void_p]
        self.x.XSetInputFocus.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
        self.xt.XTestFakeKeyEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
        self.xt.XTestFakeButtonEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
        self.xt.XTestFakeMotionEvent.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_ulong]
        self.x.XSetInputFocus(self.d,GdkX11.X11Surface.get_xid(w.get_surface()),1,0)
        self.x.XFlush(self.d)
        pump()

    def key(self,name,ctrl=False,shift=False):
        keys=['Control_L'] if ctrl else []
        if shift:keys.append('Shift_L')
        keys.append(name)
        for key in keys:
            code=self.x.XKeysymToKeycode(self.d,Gdk.keyval_from_name(key))
            self.xt.XTestFakeKeyEvent(self.d,code,1,0)
        for key in reversed(keys):
            code=self.x.XKeysymToKeycode(self.d,Gdk.keyval_from_name(key))
            self.xt.XTestFakeKeyEvent(self.d,code,0,0)
        self.x.XFlush(self.d)
        pump()

    def pointer(self,x,y,button=None):
        # Xvfb has no window manager; test windows are placed at the root origin.
        point=Graphene.Point();point.init(x,y)
        ok,p=self.w.canvas.area.compute_point(self.w,point)
        assert ok
        self.xt.XTestFakeMotionEvent(self.d,-1,round(p.x),round(p.y),0)
        if button:
            self.xt.XTestFakeButtonEvent(self.d,button,1,0)
            self.xt.XTestFakeButtonEvent(self.d,button,0,0)
        self.x.XFlush(self.d)
        pump()

    def close(self):
        self.x.XCloseDisplay(self.d)


def test_native_pointer_keyboard_paste_scroll_and_focused_shortcut_export(window):
    if not os.environ.get('DISPLAY') or os.environ.get('GDK_BACKEND')=='wayland':
        pytest.skip('Real X11 input test requires Xvfb')
    w=window
    native=NativeInput(w)
    try:
        w.canvas.area.grab_focus()
        native.key('t')
        assert w.canvas.tool=='text'
        native.pointer(*point(w,0,80,250),button=1)
        assert w.canvas.editor
        for key in 'vftdkx':native.key(key)
        assert w.canvas.editor_text()=='vftdkx'
        assert w.canvas.tool=='text'
        plain=Gdk.ContentProvider.new_for_bytes('text/plain;charset=utf-8',GLib.Bytes.new(b'Plain pasted text'))
        rich=Gdk.ContentProvider.new_for_bytes('text/html',GLib.Bytes.new(b'<h1>Styled pasted text</h1>'))
        w.get_clipboard().set_content(Gdk.ContentProvider.new_union([rich,plain]))
        native.key('a',ctrl=True)
        native.key('v',ctrl=True)
        assert w.canvas.editor_text()=='Plain pasted text'
        native.key('s',ctrl=True)
        assert w.canvas.editor is None
        assert w.last_export and w.document.marks[0].text=='Plain pasted text'
        w.canvas.area.grab_focus()
        native.pointer(*point(w,0,350,180))
        adj=w.scroll.get_vadjustment()
        before=adj.get_value()
        for _ in range(16):
            native.xt.XTestFakeButtonEvent(native.d,5,1,0)
            native.xt.XTestFakeButtonEvent(native.d,5,0,0)
            native.x.XFlush(native.d)
            pump()
        assert adj.get_value()>before
        assert w.canvas.page==1
        w.canvas.scroll_to_page(0)
        pump()
        w.canvas.area.grab_focus()
        native.key('d')
        native.pointer(*point(w,0,100,180))
        native.xt.XTestFakeButtonEvent(native.d,1,1,0)
        native.x.XFlush(native.d)
        pump()
        native.pointer(*point(w,0,150,210))
        native.xt.XTestFakeButtonEvent(native.d,1,0,0)
        native.x.XFlush(native.d)
        pump()
        assert w.document.marks[-1].kind=='draw'
        assert len(w.document.marks[-1].points)>1
        native.pointer(*point(w,0,350,180))
        old_zoom=w.canvas.zoom
        code=native.x.XKeysymToKeycode(native.d,Gdk.KEY_Control_L)
        native.xt.XTestFakeKeyEvent(native.d,code,1,0)
        native.xt.XTestFakeButtonEvent(native.d,4,1,0)
        native.xt.XTestFakeButtonEvent(native.d,4,0,0)
        native.xt.XTestFakeKeyEvent(native.d,code,0,0)
        native.x.XFlush(native.d)
        pump()
        assert w.canvas.zoom>old_zoom
        assert w.zoom_mode is None
        # Middle-drag uses viewport displacement, even as the content scrolls.
        native.pointer(*point(w,0,350,180))
        native.xt.XTestFakeButtonEvent(native.d,2,1,0)
        native.x.XFlush(native.d)
        pump()
        before=adj.get_value()
        native.pointer(*point(w,0,350,130))
        native.xt.XTestFakeButtonEvent(native.d,2,0,0)
        native.x.XFlush(native.d)
        pump()
        assert adj.get_value()>before
    finally:
        native.close()


def test_visible_page_cache_evicts_distant_backgrounds(window,tmp_path):
    path=tmp_path/'long.pdf'
    with pdf.open() as doc:
        for p in range(12):
            doc.new_page(width=500,height=650).insert_text((40,40),f'Page {p+1}')
        doc.save(path)
    w=window
    w.load(path)
    pump()
    w.zoom.set_selected(4)
    pump()
    assert 0 in w.canvas.cache
    w.canvas.scroll_to_page(11)
    pump()
    assert w.canvas.page==11
    assert 11 in w.canvas.cache and 0 not in w.canvas.cache
    assert len(w.canvas.cache)<=2


def test_password_prompt_and_text_overflow_retains_editor(window,tmp_path):
    w=window
    path=tmp_path/'secret.pdf'
    with pdf.open() as doc:
        doc.new_page(width=500,height=650)
        doc.save(path,encryption=pdf.PDF_ENCRYPT_AES_256,user_pw='secret',owner_pw='owner')
    original=w.document
    w.load(path)
    pump()
    dialog=next(x for x in w.get_application().get_windows() if isinstance(x,Gtk.Dialog))
    dialog.get_content_area().get_first_child().set_text('secret')
    dialog.response(Gtk.ResponseType.OK)
    pump()
    assert w.document is not original
    w.canvas.begin_text(Mark('text',0,(40,640,240,650),text='Too tall',size=24))
    errors=[]
    previous=w.error
    w.error=lambda title,error:errors.append(str(error))
    assert not w.canvas.finish_edit()
    assert errors and w.canvas.editor and not w.document.can_undo
    w.canvas.finish_edit(cancel=True)
    w.error=previous


def test_zoom_keeps_pointer_anchor_after_content_allocation(window):
    w=window
    adj=w.scroll.get_vadjustment()
    adj.set_value(600)
    pump()
    pointer=(300,adj.get_value()+200)
    document_y=pointer[1]/w.canvas.zoom
    w.canvas.set_zoom(2,pointer)
    pump()
    assert abs((adj.get_value()+200)/2-document_y)<1


def test_fit_modes_follow_navigation_and_window_size(window,tmp_path):
    path=tmp_path/'mixed-sizes.pdf'
    with pdf.open() as doc:
        doc.new_page(width=500,height=650)
        doc.new_page(width=700,height=400)
        doc.save(path)
    w=window
    w.load(path)
    pump()
    w.zoom.set_selected(1)
    pump()
    first=w.canvas.zoom
    w.canvas.scroll_to_page(1)
    pump()
    expected=min(w.scroll.get_width()/748,(w.scroll.get_height()-112)/400)
    assert abs(w.canvas.zoom-expected)<.01
    assert w.canvas.zoom!=first
    assert w.canvas.page==1
    w.zoom.set_selected(0)
    pump()
    w.set_default_size(600,480)
    pump()
    assert abs(w.canvas.zoom-w.scroll.get_width()/748)<.01


def test_compact_text_box_fits_content_and_shrinks_after_editing(window):
    w=window
    w.canvas.set_tool('text')
    click(w,0,80,250)
    initial=w.canvas.edit_mark.rect
    assert initial[2]-initial[0]<=120
    assert initial[3]-initial[1]<=24
    buffer=w.canvas.editor.get_buffer()
    buffer.set_text('One\nTwo\nThree')
    tall=w.canvas.edit_mark.rect[3]-w.canvas.edit_mark.rect[1]
    buffer.set_text('Signed')
    assert w.canvas.edit_mark.rect[3]-w.canvas.edit_mark.rect[1]<tall
    assert w.canvas.finish_edit()
    mark=w.document.marks[0]
    assert mark.rect[2]-mark.rect[0]<initial[2]-initial[0]
    assert mark.rect[3]-mark.rect[1]<=initial[3]-initial[1]
    assert mark.appearance
    with pdf.open(stream=w.document.pdf_bytes(),filetype='pdf') as saved:
        assert 'Signed' in saved[0].get_text()
    w.history(False)
    assert not w.document.marks
    w.history(True)
    assert w.document.marks[0]==mark


def test_fitting_text_box_preserves_wrapping_and_export(window):
    w=window
    w.canvas.set_tool('text')
    click(w,0,80,250)
    w.canvas.editor.get_buffer().set_text('Zoë signs here. This text wraps across several lines.')
    before=text_layout(w.canvas.edit_mark)
    expected=[]
    iterator=before.get_iter()
    while True:
        line=iterator.get_line_readonly()
        raw=w.canvas.editor_text().encode()
        expected.append(raw[line.start_index:line.start_index+line.length].decode().strip())
        if not iterator.next_line():break
    assert w.canvas.finish_edit()
    with pdf.open(stream=w.document.pdf_bytes(),filetype='pdf') as saved:
        lines=[line for block in saved[0].get_text('dict',clip=pdf.Rect(w.document.marks[0].rect))['blocks'] for line in block.get('lines',[])]
        assert [''.join(span['text'] for span in line['spans']).strip() for line in lines]==expected


def test_native_copy_paste_groups_preserves_marks_and_history(window):
    if not os.environ.get('DISPLAY') or os.environ.get('GDK_BACKEND')=='wayland':
        pytest.skip('Real X11 input test requires Xvfb')
    w=window
    originals=[laid_out(Mark('text',0,(60,250,200,290),text='Zoë signs',color=(.1,.2,.7))),
               Mark('draw',0,(80,320,150,350),points=((80,340),(110,320),(150,350)),width=2.5),
               Mark('tick',0,(200,300,220,320)),Mark('cross',0,(250,300,270,320))]
    w.document.add_many(originals)
    w.canvas.set_tool('select')
    w.canvas.selected=set(range(4))
    w.canvas.refresh()
    native=NativeInput(w)
    try:
        w.canvas.area.grab_focus()
        native.key('c',ctrl=True)
        assert len(w.document.marks)==4
        native.key('v',ctrl=True)
        pump()
        assert len(w.document.marks)==8
        assert w.document.marks[4:]==[m.moved(12,12) for m in originals]
        assert w.canvas.selected=={4,5,6,7}
        native.key('v',ctrl=True)
        pump()
        assert w.document.marks[8:]==[m.moved(24,24) for m in originals]
        w.history(False)
        assert len(w.document.marks)==8
        w.history(False)
        assert w.document.marks==originals
        w.history(True)
        assert w.document.marks[4:]==[m.moved(12,12) for m in originals]
        # Pasting on a different page retains searchable vector appearances.
        w.canvas.scroll_to_page(1)
        native.key('v',ctrl=True)
        pump()
        assert all(m.page==1 for m in w.document.marks[8:])
        assert w.document.marks[8].appearance==originals[0].appearance
        assert w.export()
        with pdf.open(w.last_export) as saved:
            assert 'Zoë signs' in saved[1].get_text()
        # Inside a native editor, Ctrl+C/V operates on text rather than marks.
        w.canvas.begin_text(Mark('text',1,(40,250,200,300),text='Copy this text'))
        count=len(w.document.marks)
        native.key('a',ctrl=True)
        native.key('c',ctrl=True)
        native.key('End')
        native.key('v',ctrl=True)
        assert w.canvas.editor_text()=='Copy this textCopy this text'
        assert len(w.document.marks)==count
    finally:
        native.close()


def test_paste_keeps_group_spacing_at_page_edges_and_is_atomic(window):
    w=window
    marks=[Mark('tick',0,(450,600,470,620)),Mark('cross',0,(480,600,500,620))]
    w.canvas.paste_marks(marks,0)
    first,second=w.document.marks
    assert first.rect==(450,612,470,632)
    assert second.rect==(480,612,500,632)
    assert second.rect[0]-first.rect[0]==30
    w.history(False)
    assert not w.document.marks
    with pytest.raises(ValueError,match='enough pages'):
        w.canvas.paste_marks([marks[0],Mark('cross',1,(20,20,40,40))],1)
    assert not w.document.marks


@pytest.mark.parametrize('zoom',[.75,1,2])
def test_scroll_past_last_page_keeps_navigation_and_bottom_editing(window,zoom):
    w=window
    w.canvas.set_zoom(zoom)
    pump()
    adj=w.scroll.get_vadjustment()
    last=len(w.document.pages)-1
    bottom=(w.canvas.origins[last][1]+w.document.pages[last][1])*zoom
    adj.set_value(adj.get_upper()-adj.get_page_size())
    pump()
    assert bottom-adj.get_value()<adj.get_page_size()/2
    assert w.canvas.page==last
    assert w.page_number.get_value_as_int()==last+1
    w.canvas.set_tool('text')
    click(w,last,60,625)
    w.canvas.editor.get_buffer().set_text('Near the bottom')
    assert w.canvas.finish_edit()
    assert w.document.marks[-1].page==last
    # The blank canvas is not a PDF page and cannot accept marks.
    count=len(w.document.marks)
    w.canvas.set_tool('tick')
    w.canvas.press(100,bottom+50)
    w.canvas.release(100,bottom+50)
    assert len(w.document.marks)==count


def test_scroll_padding_tracks_window_size(window):
    w=window
    w.set_default_size(600,480)
    pump()
    expected=round(w.canvas.extent[1]*w.canvas.zoom)+max(0,w.scroll.get_height()-120)
    assert w.canvas.area.get_content_height()==expected
