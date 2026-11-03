"""Continuous GTK canvas. All model and gesture coordinates are PDF points.

Page coordinates are in the PDF's displayed orientation. View coordinates are
document units on the canvas, after layout and the optional view rotation.
"""
from dataclasses import replace
from math import hypot, pi
import pymupdf as pdf
from .gtk import Gtk, Gdk, GLib, Gsk, Graphene, cairo
from .document import Mark, highlight
from .clipboard import MIME, MAX_BYTES, encode_marks, decode_marks
from .text import laid_out, fitted_text, paint_text, text_layout

GAP, TOP = 24, 72
MOVABLE = {'text', 'draw', 'tick', 'cross'}
PLACING = {'text', 'draw', 'tick', 'cross'}


def contains(rect, x, y, pad=0):
    return rect[0]-pad <= x <= rect[2]+pad and rect[1]-pad <= y <= rect[3]+pad


def pixmap_surface(pix):
    """Cairo RGB24 uses native endian BGRx on Linux."""
    data = bytearray(pix.width*pix.height*4)
    samples = pix.samples
    data[0::4], data[1::4], data[2::4] = samples[2::3], samples[1::3], samples[0::3]
    return cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, pix.width, pix.height), data


class Canvas(Gtk.Fixed):
    def __init__(self, window):
        super().__init__()
        self.set_halign(Gtk.Align.CENTER)
        self.window = window
        self.zoom, self.page, self.tool = 1.0, 0, 'select'
        self.rotation, self.spread, self.presenting = 0, 'none', False
        self.origins, self.row_top, self.rows = [], [], []
        self.cache, self.selected = {}, set()
        self.editor = self.edit_mark = self.edit_index = None
        self.gesture = None
        self.preview = {}
        self.pointer = None
        self.text_selection = None
        self.selected_annotation = None
        self.search_hits, self.search_current = [], -1
        self._zoom_anchor = None
        self._paste_payload = None
        self._paste_count = 0
        self._version = 0
        self._content_key = None
        self._wanted = {}
        self._render_source = None
        self._draw_zoom = 1.0
        self._cursor = None
        self._wheel = 0.0
        self.area = Gtk.DrawingArea(focusable=True, has_tooltip=True, accessible_role=Gtk.AccessibleRole.DOCUMENT)
        self.area.set_draw_func(self.draw)
        self.area.connect('resize', lambda *_: GLib.idle_add(self.apply_anchor))
        self.area.connect('query-tooltip', self.tooltip)
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

    # Layout and coordinate transforms.

    def reset_document(self):
        self.finish_edit(cancel=True)
        self.selected.clear()
        self.cache.clear()
        self._wanted.clear()
        self.gesture = None
        self.text_selection = self.selected_annotation = None
        self.search_hits, self.search_current = [], -1
        self.page = 0
        self.rotation, self.spread = 0, 'none'
        self.layout()
        self.refresh()

    def view_size(self, p):
        w, h = self.document.pages[p]
        return (h, w) if self.rotation % 180 else (w, h)

    def layout(self):
        count = len(self.document.pages)
        if self.spread == 'odd':
            rows = [list(range(i, min(i+2, count))) for i in range(0, count, 2)]
        elif self.spread == 'even':
            rows = [[0]] + [list(range(i, min(i+2, count))) for i in range(1, count, 2)]
        else:
            rows = [[i] for i in range(count)]
        widths = [sum(self.view_size(p)[0] for p in row) + GAP*(len(row)-1) for row in rows]
        width = max(widths)
        self.origins, self.row_top, self.rows = [None]*count, [0]*count, rows
        top = TOP
        for row, row_width in zip(rows, widths):
            height = max(self.view_size(p)[1] for p in row)
            x = GAP + (width-row_width)/2
            for p in row:
                w, h = self.view_size(p)
                self.origins[p] = (x, top + (height-h)/2)
                self.row_top[p] = top
                x += w + GAP
            top += height + GAP
        self.extent = (width+2*GAP, top+GAP)
        self.resize_canvas()

    def row_size(self, p):
        row = next(r for r in self.rows if p in r)
        return sum(self.view_size(q)[0] for q in row) + GAP*(len(row)-1), max(self.view_size(q)[1] for q in row)

    def fit_zoom(self, mode, width, height):
        if mode == 0:
            return width / self.extent[0]
        w, h = self.row_size(self.page)
        return min(width/(w+2*GAP), (height-112)/h)

    def page_to_view(self, p, x, y):
        (ox, oy), (w, h) = self.origins[p], self.document.pages[p]
        if self.rotation == 90:
            x, y = h-y, x
        elif self.rotation == 180:
            x, y = w-x, h-y
        elif self.rotation == 270:
            x, y = y, w-x
        return ox+x, oy+y

    def view_to_page(self, p, x, y):
        (ox, oy), (w, h) = self.origins[p], self.document.pages[p]
        u, v = x-ox, y-oy
        if self.rotation == 90:
            return v, h-u
        if self.rotation == 180:
            return w-u, h-v
        if self.rotation == 270:
            return w-v, u
        return u, v

    def view_rect(self, p, rect):
        (x0, y0), (x1, y1) = self.page_to_view(p, rect[0], rect[1]), self.page_to_view(p, rect[2], rect[3])
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def page_bounds(self, p):
        (ox, oy), (w, h) = self.origins[p], self.view_size(p)
        return ox, oy, ox+w, oy+h

    def page_transform(self, cr, p, origin=None):
        (ox, oy), (w, h) = origin or self.origins[p], self.document.pages[p]
        cr.translate(ox, oy)
        if self.rotation == 90:
            cr.translate(h, 0)
            cr.rotate(pi/2)
        elif self.rotation == 180:
            cr.translate(w, h)
            cr.rotate(pi)
        elif self.rotation == 270:
            cr.translate(0, w)
            cr.rotate(-pi/2)

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
        for page in range(len(self.origins)):
            if contains(self.page_bounds(page), x, y):
                return (page, *self.view_to_page(page, x, y))
        return None

    def nearest_page(self, x, y):
        def distance(p):
            x0, y0, x1, y1 = self.page_bounds(p)
            return hypot(max(x0-x, 0, x-x1), max(y0-y, 0, y-y1))
        return min(range(len(self.origins)), key=distance)

    # Hit testing in page coordinates.

    def hit(self, page, x, y):
        pad = 4/self.zoom
        for i in reversed(range(len(self.document.marks))):
            m = self.document.marks[i]
            if m.page != page:
                continue
            if m.kind == 'highlight':
                if any(contains(r, x, y, pad/2) for r in m.quads):
                    return i
            elif contains(m.rect, x, y, pad):
                return i
        return None

    def link_at(self, page, x, y):
        return next((l for l in self.document.links(page) if contains(l.rect, x, y)), None)

    def annotation_at(self, page, x, y):
        return next((a for a in reversed(self.document.page_annotations(page)) if contains(a.rect, x, y, 2/self.zoom)), None)

    def field_at(self, page, x, y, fillable=False):
        for field in reversed(self.document.fields):
            if field.page == page and contains(field.rect, x, y):
                if fillable and (field.flags & 1 or field.kind in (pdf.PDF_WIDGET_TYPE_BUTTON, pdf.PDF_WIDGET_TYPE_SIGNATURE)):
                    continue
                return field
        return None

    def refresh(self):
        self.selected.intersection_update(range(len(self.document.marks)))
        if self.selected_annotation and self.document.annotation_content(self.selected_annotation) is None:
            self.selected_annotation = None
        key = (dict(self.document.values), dict(self.document.annot_edits))
        if self._content_key != key:
            self._content_key = key
            self._version += 1
        self.area.queue_draw()
        self.window.update_state()

    def view_changed(self, *_):
        if not self.origins or self.presenting:
            return
        adj = self.window.scroll.get_vadjustment()
        top, bottom = adj.get_value()/self.zoom, (adj.get_value()+adj.get_page_size())/self.zoom
        def visible_page(p):
            y, height = self.origins[p][1], self.view_size(p)[1]
            overlap = max(0, min(bottom, y+height)-max(top, y))
            return overlap, -abs((top+bottom)/2-(y+height/2))
        page = max(range(len(self.origins)), key=visible_page)
        if page != self.page:
            self.page = page
            self.window.update_state()
        self.area.queue_draw()

    # Drawing. Page backgrounds render in idle time so scrolling stays responsive.

    def draw(self, area, cr, width, height):
        cr.set_source_rgb(*((0, 0, 0) if self.presenting else (.16, .17, .18)))
        cr.paint()
        if not self.document:
            return
        hadj, vadj = self.window.scroll.get_hadjustment(), self.window.scroll.get_vadjustment()
        wanted, visible = {}, set()
        if self.presenting:
            p = self.page
            w, h = self.view_size(p)
            vw, vh = hadj.get_page_size(), vadj.get_page_size()
            zoom = min(vw/w, vh/h)
            self._draw_zoom = zoom
            cr.translate(hadj.get_value()+(vw-w*zoom)/2, vadj.get_value()+(vh-h*zoom)/2)
            cr.scale(zoom, zoom)
            self.page_transform(cr, p, (0, 0))
            self.draw_page(cr, p, zoom, wanted, overlays=False)
            visible = {p}
            if p+1 < len(self.document.pages) and p+1 not in wanted:
                entry = self.cache.get(p+1)
                if not entry or entry[3] != self._version:
                    wanted[p+1] = min(4, zoom*self.get_scale_factor())
                visible.add(p+1)
        else:
            self._draw_zoom = self.zoom
            cr.scale(self.zoom, self.zoom)
            top = vadj.get_value()/self.zoom
            bottom = top + vadj.get_page_size()/self.zoom
            for p in range(len(self.origins)):
                x0, y0, x1, y1 = self.page_bounds(p)
                if y1 < top or y0 > bottom:
                    continue
                visible.add(p)
                cr.save()
                self.page_transform(cr, p)
                self.draw_page(cr, p, self.zoom, wanted)
                cr.restore()
        self.cache = {p: v for p, v in self.cache.items() if p in visible}
        self._wanted = wanted
        if wanted and not self._render_source:
            self._render_source = GLib.idle_add(self.render_next, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def render_next(self):
        while self._wanted and self.document:
            p = min(self._wanted, key=lambda q: abs(q-self.page))
            scale = self._wanted.pop(p)
            try:
                surface, data = pixmap_surface(self.document.render(p, scale, include_marks=False))
            except Exception:
                surface = data = None
            self.cache[p] = (scale, surface, data, self._version)
            self.area.queue_draw()
            if self._wanted:
                return True
        self._render_source = None
        return False

    def draw_page(self, cr, p, zoom, wanted, overlays=True):
        w, h = self.document.pages[p]
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        scale = min(4, zoom*self.get_scale_factor())
        entry = self.cache.get(p)
        if not entry or entry[0] != scale or entry[3] != self._version:
            wanted[p] = scale
        if entry and entry[1]:
            surface = entry[1]
            cr.save()
            cr.scale(w/surface.get_width(), h/surface.get_height())
            cr.set_source_surface(surface)
            cr.paint()
            cr.restore()
        if overlays and self.tool in ('fill', 'select'):
            for field in self.document.fields:
                if field.page == p and not field.flags & 1:
                    self.outline(cr, field.rect, wash=True)
        if overlays:
            for i, (page, rect) in enumerate(self.search_hits):
                if page == p:
                    current = i == self.search_current
                    cr.set_source_rgba(1, .55, 0, .5) if current else cr.set_source_rgba(1, .85, 0, .35)
                    cr.rectangle(rect[0], rect[1], rect[2]-rect[0], rect[3]-rect[1])
                    cr.fill()
            if selection := self.selection_range(p):
                cr.set_source_rgba(.2, .45, .95, .3)
                for x0, y0, x1, y1 in self.document.text(p).rects(*selection):
                    cr.rectangle(x0, y0, x1-x0, y1-y0)
                cr.fill()
            if self.selected_annotation and self.selected_annotation.page == p:
                self.outline(cr, self.selected_annotation.rect, color=(.95, .55, .1))
        for i, original in enumerate(self.document.marks):
            mark = self.preview.get(i, original)
            if mark.page != p or i == self.edit_index and self.editor:
                continue
            self.paint_mark(cr, mark)
            if not overlays:
                continue
            if i in self.selected:
                self.outline(cr, mark.rect, handle=mark.kind == 'text')
            elif self.tool == 'select' and mark.kind != 'highlight':
                pad = max(2/zoom, mark.width/2 if mark.kind == 'draw' else 0)
                x, y, r, b = mark.rect
                self.outline(cr, (x-pad, y-pad, r+pad, b+pad), inactive=True)
        if not overlays:
            return
        if self.editor and self.edit_mark.page == p:
            self.outline(cr, self.edit_mark.rect)
        if self.gesture and self.gesture['kind'] in ('text', 'draw') and self.gesture['page'] == p:
            g = self.gesture
            if g['kind'] == 'draw':
                self.paint_mark(cr, Mark('draw', p, (0,0,0,0), points=tuple(g['points']), width=self.window.stroke, color=self.window.ink))
            else:
                self.outline(cr, self.box(g['start'], g['end']))

    def outline(self, cr, rect, handle=False, wash=False, inactive=False, color=None):
        x, y, r, b = rect
        unit = 1/self._draw_zoom
        cr.set_source_rgba(.3, .65, 1, .12 if wash else 1)
        if inactive:
            cr.set_source_rgba(.42, .48, .55, .8)
        if color:
            cr.set_source_rgb(*color)
        cr.rectangle(x, y, r-x, b-y)
        if wash:
            cr.fill_preserve()
            cr.set_source_rgb(.2, .5, .9)
        cr.set_line_width(unit)
        cr.set_dash([] if inactive else [4*unit, 3*unit])
        cr.stroke()
        cr.set_dash([])
        if handle:
            cr.rectangle(r-4*unit, b-4*unit, 8*unit, 8*unit)
            cr.fill()

    @staticmethod
    def paint_mark(cr, mark):
        cr.save()
        x, y, r, b = mark.rect
        cr.set_source_rgb(*mark.color)
        cr.set_line_width(mark.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if mark.kind == 'highlight':
            cr.set_operator(cairo.OPERATOR_MULTIPLY)
            for x0, y0, x1, y1 in mark.quads:
                cr.rectangle(x0, y0, x1-x0, y1-y0)
            cr.fill()
        elif mark.kind == 'text':
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

    # Zoom, scrolling and view modes.

    def set_zoom(self, zoom, pointer=None):
        zoom = max(.1, min(4, zoom))
        old = self.zoom
        hadj, vadj = self.window.scroll.get_hadjustment(), self.window.scroll.get_vadjustment()
        px, py = pointer or (hadj.get_value()+hadj.get_page_size()/2, vadj.get_value()+vadj.get_page_size()/2)
        target = ((px/old*zoom - (px-hadj.get_value())), (py/old*zoom - (py-vadj.get_value())))
        self.zoom = zoom
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
        if self.presenting:
            self._wheel += dy if event.get_unit() != Gdk.ScrollUnit.SURFACE else dy/60
            if abs(self._wheel) >= 1:
                self.scroll_to_page(self.page + (1 if self._wheel > 0 else -1))
                self._wheel = 0
            return True
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

    def scroll_to_page(self, page, y=None):
        if not (self.document and 0 <= page < len(self.origins) and self.finish_edit()):
            return False
        self.page = page
        if self.presenting:
            self.area.queue_draw()
        else:
            self._zoom_anchor = None
            if self.window.zoom_mode == 1:
                self.window.apply_zoom(navigate=page)
            elif y is None:
                self.window.scroll.get_vadjustment().set_value(self.row_top[page]*self.zoom-72)
            else:
                self.window.scroll.get_vadjustment().set_value(self.page_to_view(page, 0, y)[1]*self.zoom-24)
        self.window.update_state()
        return True

    def reveal(self, page, rect):
        """Scroll a page rectangle into view, centring it when it is off-screen."""
        if self.presenting:
            self.scroll_to_page(page)
            return
        self.page = page
        hadj, vadj = self.window.scroll.get_hadjustment(), self.window.scroll.get_vadjustment()
        x0, y0, x1, y1 = (v*self.zoom for v in self.view_rect(page, rect))
        for adj, a, b in ((hadj, x0, x1), (vadj, y0, y1)):
            start, size = adj.get_value(), adj.get_page_size()
            if a < start+16 or b > start+size-16:
                adj.set_value((a+b)/2-size/2)
        self.window.update_state()
        self.area.queue_draw()

    def keep_page(self, change):
        """Apply a layout change while staying on the current page."""
        if not self.document or not self.finish_edit():
            return
        page = self.page
        change()
        self.layout()
        self.window.refit()
        self.page = page
        self._zoom_anchor = (self.window.scroll.get_hadjustment().get_value(), self.row_top[page]*self.zoom-72)
        GLib.idle_add(self.apply_anchor)
        self.refresh()

    def set_rotation(self, rotation):
        self.keep_page(lambda: setattr(self, 'rotation', rotation % 360))

    def set_spread(self, spread):
        self.keep_page(lambda: setattr(self, 'spread', spread))

    def set_tool(self, tool):
        if not self.finish_edit():
            return
        self.cancel_gesture()
        self.tool = tool
        self.selected.clear()
        self.selected_annotation = None
        if tool in PLACING:
            self.text_selection = None
        self.set_cursor('crosshair' if tool in PLACING else 'text' if tool == 'highlight' else 'default')
        self.refresh()
        self.area.grab_focus()

    def set_cursor(self, name):
        if name != self._cursor:
            self._cursor = name
            self.area.set_cursor_from_name(name)

    @staticmethod
    def box(a,b):
        return min(a[0],b[0]), min(a[1],b[1]), max(a[0],b[0]), max(a[1],b[1])

    # Text selection.

    def caret_at(self, x, y):
        """Selection caret nearest a view point, as (page, index)."""
        pos = self.to_page(x, y)
        p = pos[0] if pos else self.nearest_page(x, y)
        return p, self.document.text(p).caret(*self.view_to_page(p, x, y))

    def selection_range(self, p):
        if not self.text_selection or not self.document:
            return None
        start, end = sorted(self.text_selection)
        if start == end or not start[0] <= p <= end[0]:
            return None
        a = start[1] if p == start[0] else 0
        b = end[1] if p == end[0] else len(self.document.text(p))
        return (a, b) if a < b else None

    def selection_ranges(self):
        if not self.text_selection or not self.document:
            return []
        start, end = sorted(self.text_selection)
        return [(p, *r) for p in range(start[0], end[0]+1) if (r := self.selection_range(p))]

    def selected_text(self):
        return '\n'.join(self.document.text(p).text(a, b) for p, a, b in self.selection_ranges())

    def select_all_text(self):
        if self.document:
            last = len(self.document.pages)-1
            self.text_selection = ((0, 0), (last, len(self.document.text(last))))
            self.area.queue_draw()
            self.window.update_state()

    def copy_text(self):
        text = self.selected_text()
        if text:
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_bytes('text/plain;charset=utf-8', GLib.Bytes.new(text.encode())))
        return bool(text)

    def highlight_selection(self):
        marks = []
        for p, a, b in self.selection_ranges():
            text = self.document.text(p)
            rects = text.rects(a, b)
            if rects:
                marks.append(highlight(p, rects, self.window.highlight_color, text.text(a, b)))
        self.text_selection = None
        if marks:
            start = len(self.document.marks)
            if self.window.perform(lambda: self.document.add_many(marks)):
                self.selected = set(range(start, start+len(marks))) if self.tool == 'select' else set()
                self.refresh()

    # Pointer input.

    def pressed(self, gesture, count, x, y):
        self.press(x,y,gesture.get_current_button(), bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK), count)

    def press(self, x, y, button=1, ctrl=False, count=1):
        if not self.document:
            return
        if self.presenting:
            if button in (1, 3):
                self.scroll_to_page(self.page + (1 if button == 1 else -1))
            return
        if not self.finish_edit():
            return
        self.area.grab_focus()
        if button == 2:
            h = self.window.scroll.get_hadjustment().get_value()
            v = self.window.scroll.get_vadjustment().get_value()
            self.gesture = dict(kind='pan', start=(x-h,y-v), h=h, v=v)
            return
        view = (x/self.zoom, y/self.zoom)
        pos = self.to_page(*view)
        if not pos:
            self.selected.clear()
            self.selected_annotation = self.text_selection = None
            self.refresh()
            return
        p, x, y = pos
        index = self.hit(p,x,y)
        if button == 3:
            self.context_menu(p, x, y, index)
            return
        if button != 1:
            return
        mark = self.document.marks[index] if index is not None else None
        if mark and mark.kind in MOVABLE and self.tool in ('select','text'):
            self.text_selection = self.selected_annotation = None
            if mark.kind == 'text' and (count == 2 or self.tool == 'text'):
                self.begin_text(mark,index)
                return
            if ctrl:
                self.selected.symmetric_difference_update({index})
            elif index not in self.selected:
                self.selected = {index}
            resize = mark.kind == 'text' and hypot(x-mark.rect[2],y-mark.rect[3]) < 10/self.zoom
            self.gesture = dict(kind='resize' if resize else 'move', page=p, index=index, start=(x,y) if resize else view)
        elif self.tool in ('select', 'highlight'):
            text = self.document.text(p)
            if count >= 2 and self.tool == 'select' and text.hit(x, y, 2):
                caret = text.caret(x, y)
                a, b = text.word(caret) if count == 2 else text.line_range(caret)
                self.text_selection = ((p, a), (p, b))
                self.gesture = None
            else:
                clicked = None
                if self.tool == 'select':
                    clicked = (('mark', index) if mark else ('annotation', a) if (a := self.annotation_at(p, x, y))
                               else ('field', f) if (f := self.field_at(p, x, y, fillable=True))
                               else ('link', l) if (l := self.link_at(p, x, y)) else None)
                self.gesture = dict(kind='select-text', start=view, anchor=(p, text.caret(x, y)), moved=False, clicked=clicked, ctrl=ctrl)
        else:
            self.selected.clear()
            self.selected_annotation = self.text_selection = None
            if self.tool == 'fill':
                field = self.field_at(p, x, y)
                link = self.link_at(p, x, y)
                if field:
                    self.window.fill_field(field)
                elif link:
                    self.window.follow_link(link)
            elif self.tool in ('tick','cross'):
                s = self.window.size
                mark = Mark(self.tool,p,(x-s/2,y-s/2,x+s/2,y+s/2),size=s,width=self.window.stroke,color=self.window.ink)
                self.window.perform(lambda: self.document.add(self.fit_page(mark)))
            elif self.tool in ('text','draw'):
                self.gesture = dict(kind=self.tool,page=p,start=(x,y),end=(x,y),points=[(x,y)])
        self.refresh()

    def context_menu(self, p, x, y, index):
        vx, vy = self.page_to_view(p, x, y)
        point = (vx*self.zoom, vy*self.zoom)
        if index is not None:
            self.text_selection = self.selected_annotation = None
            if index not in self.selected:
                self.selected = {index}
            self.window.mark_menu(index, *point)
        elif (annotation := self.annotation_at(p, x, y)):
            self.selected.clear()
            self.selected_annotation = annotation
            self.window.annotation_popover(annotation, *point)
        elif self.selection_ranges():
            self.window.selection_menu(*point)
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
            self.hover(x/self.zoom, y/self.zoom)
            return
        if g['kind'] == 'pan':
            self.window.scroll.get_hadjustment().set_value(g['h']+g['start'][0]-(x-self.window.scroll.get_hadjustment().get_value()))
            self.window.scroll.get_vadjustment().set_value(g['v']+g['start'][1]-(y-self.window.scroll.get_vadjustment().get_value()))
            return
        sx,sy = x/self.zoom,y/self.zoom
        if g['kind'] == 'select-text':
            if not g['moved'] and hypot(sx-g['start'][0], sy-g['start'][1])*self.zoom < 4:
                return
            if not g['moved']:
                g['moved'] = True
                self.selected.clear()
                self.selected_annotation = None
            self.text_selection = (g['anchor'], self.caret_at(sx, sy))
        elif g['kind'] == 'move':
            dx,dy = sx-g['start'][0],sy-g['start'][1]
            changes = {}
            for i in self.selected:
                m = self.document.marks[i]
                if m.kind not in MOVABLE:
                    continue
                cx, cy = (m.rect[0]+m.rect[2])/2, (m.rect[1]+m.rect[3])/2
                vx, vy = self.page_to_view(m.page, cx, cy)
                destination = self.to_page(vx+dx, vy+dy)
                p, nx, ny = destination or (m.page, *self.view_to_page(m.page, vx+dx, vy+dy))
                changes[i] = replace(m.moved(nx-cx, ny-cy), page=p)
            self.preview = changes
        elif g['kind'] == 'resize':
            px, py = self.view_to_page(g['page'], sx, sy)
            dx, dy = px-g['start'][0], py-g['start'][1]
            m = self.document.marks[g['index']]
            w,h = self.document.pages[m.page]
            self.preview = {g['index']: replace(m, rect=(m.rect[0],m.rect[1],min(w,max(m.rect[0]+24,m.rect[2]+dx)),min(h,max(m.rect[1]+18,m.rect[3]+dy))))}
        else:
            w,h = self.document.pages[g['page']]
            px, py = self.view_to_page(g['page'], sx, sy)
            local = max(0,min(w,px)),max(0,min(h,py))
            g['end'] = local
            if local != g['points'][-1]:
                g['points'].append(local)
        self.area.queue_draw()

    def hover(self, x, y):
        if not self.document or self.presenting or self.tool in PLACING:
            return
        cursor = 'text' if self.tool == 'highlight' else 'default'
        pos = self.to_page(x, y)
        if pos and self.tool != 'highlight':
            p, px, py = pos
            index = self.hit(p, px, py)
            if index is not None and self.tool == 'select':
                cursor = 'move' if self.document.marks[index].kind in MOVABLE else 'pointer'
            elif self.link_at(p, px, py) or (self.tool == 'select' and self.annotation_at(p, px, py)):
                cursor = 'pointer'
            elif self.field_at(p, px, py, fillable=self.tool == 'select'):
                cursor = 'pointer'
            elif self.tool == 'select' and self.document.text(p).hit(px, py):
                cursor = 'text'
        self.set_cursor(cursor)

    def tooltip(self, area, x, y, keyboard, tooltip):
        if not self.document or self.presenting or keyboard:
            return False
        pos = self.to_page(x/self.zoom, y/self.zoom)
        link = pos and self.link_at(*pos)
        if link:
            tooltip.set_text(link.description)
            return True
        return False

    def released(self,gesture,count,x,y):
        self.release(x,y)

    def release(self,x,y):
        g = self.gesture
        if not g:
            return
        self.move(x,y)
        kind = g['kind']
        if kind == 'select-text':
            self.gesture = None
            if g['moved']:
                if self.tool == 'highlight':
                    self.highlight_selection()
                self.window.update_state()
                self.area.queue_draw()
                return
            self.text_selection = None
            self.selected_annotation = None
            what, item = g['clicked'] or (None, None)
            if what == 'mark':
                if g['ctrl']:
                    self.selected.symmetric_difference_update({item})
                else:
                    self.selected = {item}
            else:
                self.selected.clear()
            if what == 'annotation':
                self.selected_annotation = item
                vx, vy = self.view_rect(item.page, item.rect)[2:]
                self.window.annotation_popover(item, vx*self.zoom, vy*self.zoom)
            elif what == 'field':
                self.window.fill_field(item)
            elif what == 'link':
                self.window.follow_link(item)
            self.refresh()
            return
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

    # Inline text editing.

    def begin_text(self,mark,index=None):
        if not self.finish_edit():
            return
        self.edit_mark,self.edit_index = mark,index
        self.selected = {index} if index is not None else set()
        self.text_selection = None
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
        x, y = self.page_to_view(m.page, m.rect[0], m.rect[1])
        self.editor.set_size_request(round(m.rect[2]-m.rect[0]),round(m.rect[3]-m.rect[1]))
        point = Graphene.Point()
        point.init(x*self.zoom, y*self.zoom)
        transform = Gsk.Transform.new().translate(point).scale(self.zoom,self.zoom).rotate(self.rotation)
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

    # Selection commands.

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
        if self.selected_annotation and not self.selected:
            annotation, self.selected_annotation = self.selected_annotation, None
            self.window.perform(lambda: self.document.delete_annotation(annotation))
            return
        indices = set(self.selected)
        self.selected.clear()
        if indices:
            self.window.perform(lambda:self.document.delete_many(indices))

    def copy_selected(self):
        if not self.document or self.editor:
            return
        if not self.selected:
            self.copy_text()
            return
        marks = [self.document.marks[i] for i in sorted(self.selected) if self.document.marks[i].kind in MOVABLE]
        if not marks:
            return
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
        self.set_cursor('default')
        self.selected = set(range(start, start+len(candidates)))
        self.refresh()
        self.area.grab_focus()

    # Keyboard.

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

    def presentation_key(self, key):
        steps = {Gdk.KEY_Right: 1, Gdk.KEY_Down: 1, Gdk.KEY_Page_Down: 1, Gdk.KEY_space: 1, Gdk.KEY_Return: 1, Gdk.KEY_n: 1,
                 Gdk.KEY_Left: -1, Gdk.KEY_Up: -1, Gdk.KEY_Page_Up: -1, Gdk.KEY_BackSpace: -1, Gdk.KEY_p: -1}
        if key in steps:
            self.scroll_to_page(self.page+steps[key])
        elif key in (Gdk.KEY_Home, Gdk.KEY_End):
            self.scroll_to_page(0 if key == Gdk.KEY_Home else len(self.document.pages)-1)
        elif key in (Gdk.KEY_Escape, Gdk.KEY_F5):
            self.window.stop_presentation()
        else:
            return False
        return True

    def key(self,controller,key,code,state):
        if self.presenting:
            return self.presentation_key(key)
        control = state & Gdk.ModifierType.CONTROL_MASK
        if control and not state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            if key in (Gdk.KEY_c, Gdk.KEY_C):
                self.copy_selected()
                return True
            if key in (Gdk.KEY_v, Gdk.KEY_V):
                self.paste_elements()
                return True
            if key in (Gdk.KEY_a, Gdk.KEY_A):
                self.select_all_text()
                return True
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        name = (Gdk.keyval_name(key) or '').lower()
        tools = dict(v='select',f='fill',t='text',d='draw',k='tick',x='cross',h='highlight')
        hadj, vadj = self.window.scroll.get_hadjustment(), self.window.scroll.get_vadjustment()
        scrolls = {Gdk.KEY_Up: (vadj, -48), Gdk.KEY_Down: (vadj, 48), Gdk.KEY_Left: (hadj, -48), Gdk.KEY_Right: (hadj, 48)}
        if name in tools:
            self.set_tool(tools[name])
        elif key in (Gdk.KEY_r, Gdk.KEY_R):
            self.set_rotation(self.rotation + (-90 if key == Gdk.KEY_R else 90))
        elif key in (Gdk.KEY_Delete,Gdk.KEY_BackSpace):
            self.delete_selected()
        elif key == Gdk.KEY_Escape:
            if self.window.help.get_visible():
                self.window.toggle_help()
                return True
            self.cancel_gesture()
            self.selected.clear()
            self.selected_annotation = self.text_selection = None
            self.refresh()
        elif key == Gdk.KEY_question:
            self.window.toggle_help()
        elif key in (Gdk.KEY_Page_Up,Gdk.KEY_Page_Down):
            self.scroll_to_page(self.page+(-1 if key==Gdk.KEY_Page_Up else 1))
        elif key in (Gdk.KEY_Home, Gdk.KEY_End):
            self.scroll_to_page(0 if key == Gdk.KEY_Home else len(self.document.pages)-1)
        elif key in scrolls:
            adj, step = scrolls[key]
            adj.set_value(adj.get_value()+step)
        elif key == Gdk.KEY_space:
            vadj.set_value(vadj.get_value()+vadj.get_page_size()*(-.9 if state & Gdk.ModifierType.SHIFT_MASK else .9))
        else:
            return False
        return True
