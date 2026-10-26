"""Continuous GTK canvas. All model and gesture coordinates are PDF points."""
from dataclasses import replace
from math import hypot
from .gtk import Gtk, Gdk, GLib, Gsk, Graphene, cairo
from .document import Mark
from .clipboard import MIME, MAX_BYTES, encode_marks, decode_marks
from .text import laid_out, fitted_text, paint_text, text_layout


def contains(rect, x, y, pad=0):
    return rect[0]-pad <= x <= rect[2]+pad and rect[1]-pad <= y <= rect[3]+pad


class Canvas(Gtk.Fixed):
    def __init__(self, window):
        super().__init__()
        self.set_halign(Gtk.Align.CENTER)
        self.window = window
        self.zoom, self.page, self.tool = 1.0, 0, 'fill'
        self.origins, self.cache, self.selected = [], {}, set()
        self.editor = self.edit_mark = self.edit_index = None
        self.gesture = None
        self.preview = {}
        self.pointer = None
        self._zoom_anchor = None
        self._paste_payload = None
        self._paste_count = 0
        self.area = Gtk.DrawingArea(focusable=True)
        self.area.set_draw_func(self.draw)
        self.area.connect('resize',lambda *_:GLib.idle_add(self.apply_anchor))
        self.put(self.area, 0, 0)
        click = Gtk.GestureClick(button=0)
        click.connect('pressed', self.pressed)
        click.connect('released', self.released)
        self.area.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda _, x, y: self.move(x, y))
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key)
        self.area.add_controller(keys)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect('scroll', self.wheel)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(scroll)

    @property
    def document(self):
        return self.window.document

    @property
    def pending(self):
        return self.editor is not None and (self.edit_index is not None or bool(self.editor_text()))

    def editor_text(self):
        b = self.editor.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), True)

    def reset_document(self):
        self.finish_edit(cancel=True)
        self.selected.clear()
        self.cache.clear()
        self.gesture = None
        self.page = 0
        self.origins = []
        width = max(w for w, h in self.document.pages)
        top = 72
        for w, h in self.document.pages:
            self.origins.append((24 + (width-w)/2, top))
            top += h + 24
        self.extent = (width+48, top+24)
        self.resize_canvas()
        self.refresh()

    def resize_canvas(self):
        if not self.origins:
            return
        w, h = (round(v*self.zoom) for v in self.extent)
        # Let the last page's bottom edge move up to the editing area.
        h += max(0, self.window.scroll.get_height()-120)
        self.area.set_content_width(w)
        self.area.set_content_height(h)
        self.set_size_request(w, h)
        self.position_editor()

    def to_page(self, x, y):
        for page, ((ox, oy), (w, h)) in enumerate(zip(self.origins, self.document.pages)):
            if contains((ox, oy, ox+w, oy+h), x, y):
                return page, x-ox, y-oy
        return None

    def hit(self, page, x, y):
        return next((i for i in reversed(range(len(self.document.marks)))
                     if self.document.marks[i].page == page and contains(self.document.marks[i].rect, x, y, 4/self.zoom)), None)

    def refresh(self):
        self.selected.intersection_update(range(len(self.document.marks)))
        if getattr(self, '_values', None) != self.document.values:
            self._values = dict(self.document.values)
            self.cache.clear()
        self.area.queue_draw()
        self.window.update_state()

    def view_changed(self, *_):
        if not self.origins:
            return
        adj = self.window.scroll.get_vadjustment()
        top, bottom = adj.get_value()/self.zoom, (adj.get_value()+adj.get_page_size())/self.zoom
        def visible_page(p):
            y, height = self.origins[p][1], self.document.pages[p][1]
            overlap = max(0, min(bottom, y+height)-max(top, y))
            return overlap, -abs((top+bottom)/2-(y+height/2))
        self.page = max(range(len(self.origins)), key=visible_page)
        self.window.update_state()
        self.area.queue_draw()

    def draw(self, area, cr, width, height):
        cr.set_source_rgb(.16, .17, .18)
        cr.paint()
        if not self.document:
            return
        cr.scale(self.zoom, self.zoom)
        adj = self.window.scroll.get_vadjustment()
        top = adj.get_value()/self.zoom
        bottom = top + adj.get_page_size()/self.zoom
        visible = set()
        for p, ((ox, oy), (w, h)) in enumerate(zip(self.origins, self.document.pages)):
            if oy+h < top or oy > bottom:
                continue
            visible.add(p)
            cr.save()
            cr.translate(ox, oy)
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            scale = min(4, self.zoom * self.get_scale_factor())
            if p not in self.cache or self.cache[p][0] != scale:
                pix = self.document.render(p, scale, include_marks=False)
                # Cairo RGB24 uses native endian BGRx on Linux.
                rgba = bytearray(pix.width*pix.height*4)
                samples = pix.samples
                rgba[0::4], rgba[1::4], rgba[2::4] = samples[2::3], samples[1::3], samples[0::3]
                surface = cairo.ImageSurface.create_for_data(rgba, cairo.FORMAT_RGB24, pix.width, pix.height)
                self.cache[p] = (scale, surface, rgba)
            _, surface, _data = self.cache[p]
            cr.save()
            cr.scale(w/surface.get_width(), h/surface.get_height())
            cr.set_source_surface(surface)
            cr.paint()
            cr.restore()
            if self.tool == 'fill':
                for field in self.document.fields:
                    if field.page == p and not field.flags & 1:
                        self.outline(cr, field.rect, wash=True)
            for i, original in enumerate(self.document.marks):
                mark = self.preview.get(i, original)
                if mark.page != p or i == self.edit_index and self.editor:
                    continue
                self.paint_mark(cr, mark)
                if i in self.selected:
                    self.outline(cr, mark.rect, handle=mark.kind == 'text')
                elif self.tool == 'select':
                    pad = max(2/self.zoom, mark.width/2 if mark.kind == 'draw' else 0)
                    x, y, r, b = mark.rect
                    self.outline(cr, (x-pad, y-pad, r+pad, b+pad), inactive=True)
            if self.editor and self.edit_mark.page == p:
                self.outline(cr, self.edit_mark.rect)
            if self.gesture and self.gesture['kind'] in ('text', 'draw') and self.gesture['page'] == p:
                g = self.gesture
                if g['kind'] == 'draw':
                    self.paint_mark(cr, Mark('draw', p, (0,0,0,0), points=tuple(g['points']), width=self.window.stroke, color=self.window.ink))
                else:
                    self.outline(cr, self.box(g['start'], g['end']))
            cr.restore()
        self.cache = {p: v for p, v in self.cache.items() if p in visible}

    def outline(self, cr, rect, handle=False, wash=False, inactive=False):
        x, y, r, b = rect
        cr.set_source_rgba(.3, .65, 1, .12 if wash else 1)
        if inactive:
            cr.set_source_rgba(.42, .48, .55, .8)
        cr.rectangle(x, y, r-x, b-y)
        if wash:
            cr.fill_preserve()
            cr.set_source_rgb(.2, .5, .9)
        cr.set_line_width(1/self.zoom)
        cr.set_dash([] if inactive else [4/self.zoom, 3/self.zoom])
        cr.stroke()
        cr.set_dash([])
        if handle:
            cr.rectangle(r-4/self.zoom, b-4/self.zoom, 8/self.zoom, 8/self.zoom)
            cr.fill()

    @staticmethod
    def paint_mark(cr, mark):
        cr.save()
        x, y, r, b = mark.rect
        cr.set_source_rgb(*mark.color)
        cr.set_line_width(mark.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if mark.kind == 'text':
            cr.translate(x, y)
            paint_text(cr, mark)
        else:
            points = mark.points if mark.kind == 'draw' else ((x,y+(b-y)*.52),(x+(r-x)*.36,b),(r,y)) if mark.kind == 'tick' else ((x,y),(r,b))
            if len(points) == 1:
                cr.arc(*points[0], mark.width/2, 0, 6.2832)
                cr.fill()
            elif points:
                cr.move_to(*points[0])
                for point in points[1:]:
                    cr.line_to(*point)
                if mark.kind == 'cross':
                    cr.move_to(r,y)
                    cr.line_to(x,b)
                cr.stroke()
        cr.restore()

    def set_zoom(self, zoom, pointer=None):
        zoom = max(.1, min(4, zoom))
        old = self.zoom
        hadj, vadj = self.window.scroll.get_hadjustment(), self.window.scroll.get_vadjustment()
        px, py = pointer or (hadj.get_value()+hadj.get_page_size()/2, vadj.get_value()+vadj.get_page_size()/2)
        target = ((px/old*zoom - (px-hadj.get_value())), (py/old*zoom - (py-vadj.get_value())))
        self.zoom = zoom
        self.cache.clear()
        self.resize_canvas()
        self._zoom_anchor = target
        GLib.idle_add(self.apply_anchor)
        self.refresh()

    def apply_anchor(self):
        # Wait until the new content allocation has updated scroll bounds.
        # Applying earlier clamps a zoom-in anchor to the previous page extent.
        if self._zoom_anchor and (self.area.get_width(), self.area.get_height()) == (self.area.get_content_width(), self.area.get_content_height()):
            h, v = self._zoom_anchor
            self._zoom_anchor = None
            self.window.scroll.get_hadjustment().set_value(h)
            self.window.scroll.get_vadjustment().set_value(v)
        return False

    def wheel(self, controller, dx, dy):
        hadj, vadj = self.window.scroll.get_hadjustment(), self.window.scroll.get_vadjustment()
        event = controller.get_current_event()
        if controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            # Discrete X11 scroll events have no position; remember the last
            # pointer location in viewport coordinates on both display backends.
            pointer = (self.pointer[0]+hadj.get_value(), self.pointer[1]+vadj.get_value()) if self.pointer else None
            self.window.zoom_mode = None
            self.set_zoom(self.zoom * (1.1 ** -dy), pointer)
        else:
            step = 1 if event.get_unit() == Gdk.ScrollUnit.SURFACE else 64
            if controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK and not dx:
                dx, dy = dy, 0
            hadj.set_value(hadj.get_value()+dx*step)
            vadj.set_value(vadj.get_value()+dy*step)
        return True

    def scroll_to_page(self, page):
        if self.document and 0 <= page < len(self.origins) and self.finish_edit():
            self.page = page
            self._zoom_anchor = None
            if self.window.zoom_mode == 1:
                self.window.apply_zoom(navigate=page)
            else:
                self.window.scroll.get_vadjustment().set_value(self.origins[page][1]*self.zoom-72)
            self.window.update_state()

    def set_tool(self, tool):
        if not self.finish_edit():
            return
        self.cancel_gesture()
        self.tool = tool
        self.selected.clear()
        self.area.set_cursor_from_name('crosshair' if tool in ('text','draw','tick','cross') else 'default')
        self.refresh()
        self.area.grab_focus()

    @staticmethod
    def box(a,b):
        return min(a[0],b[0]), min(a[1],b[1]), max(a[0],b[0]), max(a[1],b[1])

    def pressed(self, gesture, count, x, y):
        self.press(x,y,gesture.get_current_button(), bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK), count)

    def press(self, x, y, button=1, ctrl=False, count=1):
        if not self.document or not self.finish_edit():
            return
        self.area.grab_focus()
        if button == 2:
            h = self.window.scroll.get_hadjustment().get_value()
            v = self.window.scroll.get_vadjustment().get_value()
            self.gesture = dict(kind='pan', start=(x-h,y-v), h=h, v=v)
            return
        pos = self.to_page(x/self.zoom, y/self.zoom)
        if not pos:
            self.selected.clear()
            self.refresh()
            return
        p, x, y = pos
        index = self.hit(p,x,y)
        if button == 3:
            if index is not None:
                self.selected = {index}
                self.window.mark_menu(index, (x+self.origins[p][0])*self.zoom, (y+self.origins[p][1])*self.zoom)
            self.refresh()
            return
        if button != 1:
            return
        if index is not None and self.tool in ('select','text'):
            mark = self.document.marks[index]
            if mark.kind == 'text' and (count == 2 or self.tool == 'text'):
                self.begin_text(mark,index)
                return
            if ctrl:
                self.selected.symmetric_difference_update({index})
            elif index not in self.selected:
                self.selected = {index}
            resize = mark.kind == 'text' and hypot(x-mark.rect[2],y-mark.rect[3]) < 10/self.zoom
            self.gesture = dict(kind='resize' if resize else 'move', page=p, index=index, start=(x+self.origins[p][0],y+self.origins[p][1]))
        else:
            self.selected.clear()
            if self.tool == 'fill':
                for field in reversed(self.document.fields):
                    if field.page == p and contains(field.rect,x,y):
                        self.window.fill_field(field)
                        break
            elif self.tool in ('tick','cross'):
                s = self.window.size
                mark = Mark(self.tool,p,(x-s/2,y-s/2,x+s/2,y+s/2),size=s,width=self.window.stroke,color=self.window.ink)
                self.window.perform(lambda: self.document.add(self.fit_page(mark)))
            elif self.tool in ('text','draw'):
                self.gesture = dict(kind=self.tool,page=p,start=(x,y),end=(x,y),points=[(x,y)])
        self.refresh()

    def fit_page(self, mark):
        w,h = self.document.pages[mark.page]
        x,y,r,b = mark.rect
        if r-x > w or b-y > h:
            raise ValueError('This mark does not fit on the page. Use a smaller size or a wider text box.')
        return mark.moved(max(0,min(x,w-(r-x)))-x,max(0,min(y,h-(b-y)))-y)

    def move(self,x,y):
        self.pointer = (x-self.window.scroll.get_hadjustment().get_value(), y-self.window.scroll.get_vadjustment().get_value())
        g = self.gesture
        if not g:
            return
        if g['kind'] == 'pan':
            self.window.scroll.get_hadjustment().set_value(g['h']+g['start'][0]-(x-self.window.scroll.get_hadjustment().get_value()))
            self.window.scroll.get_vadjustment().set_value(g['v']+g['start'][1]-(y-self.window.scroll.get_vadjustment().get_value()))
            return
        sx,sy = x/self.zoom,y/self.zoom
        if g['kind'] in ('move','resize'):
            dx,dy = sx-g['start'][0],sy-g['start'][1]
            changes = {}
            for i in self.selected:
                m = self.document.marks[i]
                if g['kind'] == 'resize':
                    if i != g['index']:
                        continue
                    w,h = self.document.pages[m.page]
                    changes[i] = replace(m, rect=(m.rect[0],m.rect[1],min(w,max(m.rect[0]+24,m.rect[2]+dx)),min(h,max(m.rect[1]+18,m.rect[3]+dy))))
                else:
                    ox,oy = self.origins[m.page]
                    destination = self.to_page(ox+(m.rect[0]+m.rect[2])/2+dx,oy+(m.rect[1]+m.rect[3])/2+dy)
                    p = destination[0] if destination else m.page
                    nx,ny = self.origins[p]
                    changes[i] = replace(m.moved(dx+ox-nx,dy+oy-ny),page=p)
            self.preview = changes
        else:
            ox,oy = self.origins[g['page']]
            w,h = self.document.pages[g['page']]
            local = max(0,min(w,sx-ox)),max(0,min(h,sy-oy))
            g['end'] = local
            if local != g['points'][-1]:
                g['points'].append(local)
        self.area.queue_draw()

    def released(self,gesture,count,x,y):
        self.release(x,y)

    def release(self,x,y):
        g = self.gesture
        if not g:
            return
        self.move(x,y)
        kind = g['kind']
        if kind in ('move','resize'):
            def apply():
                changes = {i:self.fit_page(laid_out(m) if m.kind=='text' else m) for i,m in self.preview.items()}
                self.document.update_many(changes)
            self.window.perform(apply)
        elif kind == 'draw':
            xs,ys = zip(*g['points'])
            mark = Mark('draw',g['page'],(min(xs),min(ys),max(xs),max(ys)),points=tuple(g['points']),width=self.window.stroke,color=self.window.ink)
            self.window.perform(lambda:self.document.add(mark))
        elif kind == 'text':
            rect = self.box(g['start'],g['end'])
            if rect[2]-rect[0]<8 and rect[3]-rect[1]<8:
                x,y = g['start']
                w,h = self.document.pages[g['page']]
                height = max(20, self.window.size*1.5)
                x,y = min(x,max(0,w-120)),min(y,max(0,h-height))
                rect = (x,y,min(w,x+120),min(h,y+height))
            if rect[2]-rect[0]>=8 and rect[3]-rect[1]>=8:
                self.begin_text(Mark('text',g['page'],rect,size=self.window.size,color=self.window.ink))
        self.cancel_gesture()

    def cancel_gesture(self):
        self.gesture = None
        self.preview = {}
        self.area.queue_draw()

    def begin_text(self,mark,index=None):
        if not self.finish_edit():
            return
        self.edit_mark,self.edit_index = mark,index
        self.selected = {index} if index is not None else set()
        if hasattr(self, '_editor_css'):
            del self._editor_css
        self.editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=True)
        self.editor.add_css_class('inline-editor')
        self.editor.get_buffer().set_enable_undo(True)
        self.editor.get_buffer().set_text(mark.text)
        self.editor.get_buffer().connect('changed',lambda *_:self.editor_changed())
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed',self.editor_key)
        self.editor.add_controller(keys)
        self.put(self.editor,0,0)
        self.style_editor()
        self.editor.grab_focus()
        self.refresh()

    def style_editor(self):
        m = self.edit_mark
        color = ','.join(str(round(c*255)) for c in m.color)
        provider = Gtk.CssProvider()
        provider.load_from_data(f'textview.inline-editor, textview.inline-editor text {{font-family: "DejaVu Sans"; font-size: {m.size}px; color: rgb({color}); background: white; padding: 0; margin: 0; border: 0;}}'.encode())
        if hasattr(self, '_editor_css'):
            self.editor.get_style_context().remove_provider(self._editor_css)
        self._editor_css = provider
        self.editor.get_style_context().add_provider(provider,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION+1)
        self.editor_changed()

    def editor_changed(self):
        if not self.editor:
            return
        m = replace(self.edit_mark,text=self.editor_text())
        h = text_layout(m).get_pixel_size()[1]
        self.edit_mark = replace(m,rect=(*m.rect[:3],m.rect[1]+h))
        self.position_editor()
        self.window.update_state()

    def position_editor(self):
        if not self.editor:
            return
        m = self.edit_mark
        ox,oy = self.origins[m.page]
        self.editor.set_size_request(round(m.rect[2]-m.rect[0]),round(m.rect[3]-m.rect[1]))
        point = Graphene.Point()
        point.init((ox+m.rect[0])*self.zoom,(oy+m.rect[1])*self.zoom)
        transform = Gsk.Transform.new().translate(point).scale(self.zoom,self.zoom)
        self.set_child_transform(self.editor,transform)

    def finish_edit(self,cancel=False):
        if not self.editor:
            return True
        index = self.edit_index
        if not cancel:
            try:
                m = replace(self.edit_mark,text=self.editor_text())
                if m.text.strip():
                    m = fitted_text(m)
                    w,h = self.document.pages[m.page]
                    if m.rect[3] > h:
                        raise ValueError('Text extends beyond this page. Move the box up or use a smaller size.')
                    if index is None:
                        self.document.add(m)
                        index = len(self.document.marks)-1
                    elif (m.text,m.rect,m.size,m.color) != (self.document.marks[index].text,self.document.marks[index].rect,self.document.marks[index].size,self.document.marks[index].color):
                        self.document.update(index,m)
                elif index is not None:
                    self.document.delete(index)
                    index = None
            except Exception as error:
                self.window.error('Could not finish text',error)
                self.editor.grab_focus()
                return False
        self.remove(self.editor)
        self.editor = self.edit_mark = self.edit_index = None
        self.selected = {index} if index is not None else set()
        self.refresh()
        return True

    def style_selected(self,**style):
        def changed(m):
            size = style.get('size')
            if size is not None and m.kind in ('tick','cross'):
                m = replace(m,rect=(m.rect[0],m.rect[1],m.rect[0]+size,m.rect[1]+size))
            m = replace(m,**style)
            return m
        if self.editor:
            self.edit_mark = changed(self.edit_mark)
            self.style_editor()
        elif self.selected:
            def apply():
                changes = {}
                for i in self.selected:
                    m = changed(self.document.marks[i])
                    changes[i] = self.fit_page(laid_out(m) if m.kind=='text' else m)
                self.document.update_many(changes)
            self.window.perform(apply)

    def delete_selected(self):
        if not self.finish_edit():
            return
        indices = set(self.selected)
        self.selected.clear()
        if indices:
            self.window.perform(lambda:self.document.delete_many(indices))

    def copy_selected(self):
        if not self.document or self.editor or not self.selected:
            return
        marks = [self.document.marks[i] for i in sorted(self.selected)]
        payload = encode_marks(marks)
        if len(payload) > MAX_BYTES:
            self.window.notify('This selection is too large to copy.')
            return
        providers = [Gdk.ContentProvider.new_for_bytes(MIME, GLib.Bytes.new(payload))]
        text = '\n'.join(m.text for m in marks if m.kind == 'text')
        if text:
            providers.append(Gdk.ContentProvider.new_for_bytes('text/plain;charset=utf-8', GLib.Bytes.new(text.encode())))
        self.get_clipboard().set_content(Gdk.ContentProvider.new_union(providers))
        self._paste_payload, self._paste_count = payload, 0

    def paste_elements(self):
        clipboard = self.get_clipboard()
        if not self.document or self.editor or not clipboard.get_formats().contain_mime_type(MIME):
            return
        document, page = self.document, self.page
        chunks, total = [], 0

        def failed(error):
            self.window.error('Could not paste elements', error)

        def read(stream, result):
            nonlocal total
            try:
                block = stream.read_bytes_finish(result).get_data()
                total += len(block)
                if total > MAX_BYTES:
                    raise ValueError('The clipboard selection is too large.')
                if block:
                    chunks.append(block)
                    stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, read)
                    return
                stream.close(None)
                if self.document is document and not self.editor:
                    payload = b''.join(chunks)
                    count = self._paste_count+1 if payload == self._paste_payload else 1
                    self.paste_marks(decode_marks(payload), page, count*12)
                    self._paste_payload, self._paste_count = payload, count
            except Exception as error:
                stream.close(None)
                failed(error)

        def opened(clipboard, result):
            try:
                stream, mime = clipboard.read_finish(result)
                stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, read)
            except Exception as error:
                failed(error)

        clipboard.read_async([MIME], GLib.PRIORITY_DEFAULT, None, opened)

    def paste_marks(self, marks, page, offset=12):
        """Paste on the current page; keep group spacing and one undo step."""
        first_page = min(mark.page for mark in marks)
        candidates = [replace(mark.moved(offset, offset), page=page+mark.page-first_page) for mark in marks]
        for destination in {mark.page for mark in candidates}:
            if not 0 <= destination < len(self.document.pages):
                raise ValueError('There are not enough pages for this selection.')
            group = [mark for mark in candidates if mark.page == destination]
            x, y = min(m.rect[0] for m in group), min(m.rect[1] for m in group)
            r, b = max(m.rect[2] for m in group), max(m.rect[3] for m in group)
            w, h = self.document.pages[destination]
            if r-x > w or b-y > h:
                raise ValueError('This selection does not fit on the destination page.')
            dx, dy = max(0, min(x, w-(r-x)))-x, max(0, min(y, h-(b-y)))-y
            candidates = [m.moved(dx, dy) if m.page == destination else m for m in candidates]
        start = len(self.document.marks)
        self.document.add_many(candidates)
        self.cancel_gesture()
        self.tool = 'select'
        self.area.set_cursor_from_name('default')
        self.selected = set(range(start, start+len(candidates)))
        self.refresh()
        self.area.grab_focus()

    def editor_key(self,controller,key,code,state):
        if key == Gdk.KEY_Escape:
            self.finish_edit(cancel=True)
            self.area.grab_focus()
            return True
        if key == Gdk.KEY_Return and state & Gdk.ModifierType.CONTROL_MASK:
            self.finish_edit()
            self.area.grab_focus()
            return True
        return False

    def key(self,controller,key,code,state):
        if state & Gdk.ModifierType.CONTROL_MASK and not state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            if key in (Gdk.KEY_c, Gdk.KEY_C):
                self.copy_selected()
                return True
            if key in (Gdk.KEY_v, Gdk.KEY_V):
                self.paste_elements()
                return True
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        name = Gdk.keyval_name(key).lower()
        tools = dict(v='select',f='fill',t='text',d='draw',k='tick',x='cross')
        if name in tools:
            self.set_tool(tools[name])
        elif key in (Gdk.KEY_Delete,Gdk.KEY_BackSpace):
            self.delete_selected()
        elif key == Gdk.KEY_Escape:
            if self.window.help.get_visible():
                self.window.toggle_help()
                return True
            self.cancel_gesture()
            self.selected.clear()
            self.refresh()
        elif key == Gdk.KEY_question:
            self.window.toggle_help()
        elif key in (Gdk.KEY_Page_Up,Gdk.KEY_Page_Down):
            self.scroll_to_page(self.page+(-1 if key==Gdk.KEY_Page_Up else 1))
        else:
            return False
        return True
