"""Draw laid-out XFA pages into a PDF, with AcroForm widgets for editable fields.

Read-only values become ordinary page text, so they can be searched, selected
and printed exactly as laid out. Editable fields become standard form fields.
"""
from __future__ import annotations

import base64
import binascii
import math
import re
from dataclasses import dataclass

import pymupdf as pdf

from . import text as tx
from .layout import Box, Page, comb_cells, multiline, paragraphs_for
from .model import Font, Outline, Stroke, access_of, margin_of, measure, on_value, outline_of, para_of, ui_of
from .values import check_state, display

RADIO = 1 << 15 | 1 << 14  # Radio, and no toggling off.
TEXT_KINDS = ("textEdit", "numericEdit", "dateTimeEdit", "passwordEdit")
EDITABLE = TEXT_KINDS + ("choiceList", "checkButton")
# Designer's default object names say nothing about what a field is for.
GENERIC = re.compile(r"(TextField|NumericField|DecimalField|DateTimeField|CheckBox|RadioButton|DropDownList|"
                     r"ListBox|Button|Text|Field)\d*", re.I)


@dataclass
class WidgetSpec:
    rect: tuple
    kind: str
    name: str
    label: str
    value: object
    font: Font
    align: str
    flags: int = 0
    max_length: int = 0
    choices: tuple = ()
    on: str = ""
    radio: bool = False


class Canvas:
    """Drawing operations in paint order, written to the page in batches."""

    def __init__(self, page: pdf.Page):
        self.page = page
        self.ops: list[tuple[str, object]] = []

    def shape(self, fn):
        self.ops.append(("shape", fn))

    def text(self, x: float, y: float, value: str, style: Font):
        self.ops.append(("text", (x, y, value, style)))

    def image(self, rect, data: bytes, keep: bool):
        self.ops.append(("image", (rect, data, keep)))

    def flush(self):
        ops, i = self.ops, 0
        while i < len(ops):
            kind, j = ops[i][0], i
            while j < len(ops) and ops[j][0] == kind:
                j += 1
            batch = ops[i:j]
            if kind == "shape":
                shape = self.page.new_shape()
                for _, fn in batch:
                    fn(shape)
                shape.commit()
            elif kind == "text":
                writers: dict[tuple, pdf.TextWriter] = {}
                for _, (x, y, value, style) in batch:
                    writer = writers.get(style.color)
                    if writer is None:
                        writer = writers[style.color] = pdf.TextWriter(self.page.rect, color=style.color)
                    font = tx.face_of(style).font
                    if style.letter_spacing:
                        for ch in value:
                            writer.append((x, y), ch, font=font, fontsize=style.size)
                            x += font.text_length(ch, fontsize=style.size) + style.letter_spacing
                    else:
                        writer.append((x, y), value, font=font, fontsize=style.size)
                for writer in writers.values():
                    writer.write_text(self.page)
            else:
                for _, (rect, data, keep) in batch:
                    try:
                        self.page.insert_image(pdf.Rect(rect), stream=data, keep_proportion=keep)
                    except Exception:
                        pass  # A broken image should not stop the rest of the form.
            i = j
        self.ops = []


def dashes(stroke: Stroke) -> str | None:
    t = max(stroke.thickness, 0.5)
    return {"dashed": f"[{3 * t:g} {3 * t:g}] 0", "dotted": f"[{t:g} {t:g}] 0",
            "dashDot": f"[{3 * t:g} {t:g} {t:g} {t:g}] 0",
            "dashDotDot": f"[{3 * t:g} {t:g} {t:g} {t:g} {t:g} {t:g}] 0"}.get(stroke.style)


def paint_outline(canvas: Canvas, rect, outline: Outline | None, oval: bool = False):
    if outline is None or not outline.visible:
        return
    x0, y0, x1, y1 = rect
    edges = outline.edges
    shown = [e for e in edges if e.visible and e.thickness > 0]
    t = max((e.thickness for e in shown), default=0.0)
    inset = {"right": t / 2, "left": -t / 2}.get(outline.hand, 0.0)
    r = pdf.Rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset)
    if r.width < 0 or r.height < 0:
        r = pdf.Rect(x0, y0, x1, y1)
    radius = None
    if outline.radius > 0 and r.width > 0 and r.height > 0:
        radius = (min(0.5, outline.radius / r.width), min(0.5, outline.radius / r.height))
    uniform = len(shown) == 4 and len(set(edges)) == 1
    fill = outline.fill

    def path(shape, area):
        if oval:
            shape.draw_oval(area)
        else:
            shape.draw_rect(area, radius=radius)

    if uniform:
        edge = edges[0]

        def draw(shape):
            path(shape, r)
            shape.finish(color=edge.color, fill=fill, width=edge.thickness, dashes=dashes(edge),
                         lineJoin=1 if radius else 0, closePath=True)
        canvas.shape(draw)
        if edge.style in ("lowered", "raised") and not oval and r.width > 2 * t and r.height > 2 * t:
            bevel(canvas, pdf.Rect(r.x0 + t, r.y0 + t, r.x1 - t, r.y1 - t), t, edge.style == "lowered")
        return
    if fill is not None:
        def draw_fill(shape):
            path(shape, r)
            shape.finish(color=None, fill=fill, width=0)
        canvas.shape(draw_fill)
    if oval:
        if shown:
            edge = shown[0]
            canvas.shape(lambda shape: (shape.draw_oval(r), shape.finish(color=edge.color, width=edge.thickness)))
        return
    sides = ((r.tl, r.tr), (r.tr, r.br), (r.br, r.bl), (r.bl, r.tl))
    for edge, (a, b) in zip(edges, sides):
        if edge.visible and edge.thickness > 0:
            canvas.shape(lambda shape, a=a, b=b, e=edge: (
                shape.draw_line(a, b), shape.finish(color=e.color, width=e.thickness, dashes=dashes(e), lineCap=2)))


def paint_arc(canvas: Canvas, rect, el):
    """An ellipse, or part of one: angles run anticlockwise from three o'clock.
    A filled partial arc fills the area between the arc and its chord."""
    outline = outline_of(el, white=True)
    if outline is None or not outline.visible:
        return
    edge = outline.edges[0]
    width = edge.thickness if edge.visible else 0.0
    inset = {"right": width / 2, "left": -width / 2}.get(outline.hand, 0.0)
    x0, y0, x1, y1 = rect
    rx, ry = (x1 - x0) / 2 - inset, (y1 - y0) / 2 - inset
    if el.get("circular") == "1":
        rx = ry = min(rx, ry)
    if rx <= 0 or ry <= 0:
        return
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    start, sweep = number(el.get("startAngle"), 0.0), number(el.get("sweepAngle"), 360.0)
    style = dict(color=edge.color if width else None, fill=outline.fill, width=width, dashes=dashes(edge))
    if abs(sweep) >= 360:
        canvas.shape(lambda shape: (shape.draw_oval(pdf.Rect(cx - rx, cy - ry, cx + rx, cy + ry)), shape.finish(**style)))
        return

    def point(angle):
        return pdf.Point(cx + rx * math.cos(angle), cy - ry * math.sin(angle))

    def draw(shape):
        steps = max(1, math.ceil(abs(sweep) / 90))
        delta = math.radians(sweep) / steps
        k = 4 / 3 * math.tan(delta / 4)
        angle = math.radians(start)
        for _ in range(steps):
            a, b = angle, angle + delta
            p0, p3 = point(a), point(b)
            p1 = p0 + (-k * rx * math.sin(a), -k * ry * math.cos(a))
            p2 = p3 - (-k * rx * math.sin(b), -k * ry * math.cos(b))
            shape.draw_bezier(p0, p1, p2, p3)
            angle = b
        shape.finish(closePath=False, **style)
    canvas.shape(draw)


def number(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def bevel(canvas: Canvas, r: pdf.Rect, t: float, lowered: bool):
    dark, light = (0.5, 0.5, 0.5), (0.83, 0.83, 0.83)
    first, second = (dark, light) if lowered else (light, dark)

    def draw(shape):
        shape.draw_polyline([r.bl, r.tl, r.tr])
        shape.finish(color=first, width=t, closePath=False)
        shape.draw_polyline([r.tr, r.br, r.bl])
        shape.finish(color=second, width=t, closePath=False)
    canvas.shape(draw)


def paint_block(canvas: Canvas, block: tx.Block, rect, valign: str):
    x0, y0, x1, y1 = rect
    width = x1 - x0
    total = block.height
    y = y0 if valign == "top" else y0 + (y1 - y0 - total) / 2 if valign == "middle" else y1 - total
    for line in block.lines:
        para = line.para
        if line.first:
            y += para.above
        baseline = y + (line.height - line.ascent - line.descent) / 2 + line.ascent
        indent = para.indent if line.first else 0.0
        avail = width - para.left - para.right - indent
        offset, extra = 0.0, 0.0
        if para.h == "center":
            offset = (avail - line.width) / 2
        elif para.h in ("right", "radix"):
            offset = avail - line.width
        elif para.h in ("justify", "justifyAll") and (not line.last or para.h == "justifyAll"):
            gaps = sum(len(re.findall(r" +", p.text.rstrip() if p is line.pieces[-1] else p.text)) for p in line.pieces)
            if gaps:
                extra = max(0.0, avail - line.width) / gaps
        if y + line.height > y0 - 0.5 and y < y1 + 0.5:  # Lines wholly outside the box are clipped.
            shift = 0.0
            for piece in line.pieces:
                x = x0 + para.left + indent + offset + piece.x
                base = baseline - piece.style.baseline_shift
                if not extra:
                    if piece.text.strip():
                        canvas.text(x, base, piece.text, piece.style)
                        decorate(canvas, x, base, piece)
                    continue
                for part in re.split(r"( +)", piece.text):
                    if part.startswith(" "):
                        shift += extra
                    elif part:
                        canvas.text(x + shift, base, part, piece.style)
                    x += tx.width_of(part, piece.style)
        y += line.height
        if line.last:
            y += para.below


def decorate(canvas: Canvas, x: float, baseline: float, piece: tx.Piece):
    style = piece.style
    lines = []
    if style.underline:
        lines.append(baseline + style.size * 0.12)
    if style.strike:
        lines.append(baseline - style.size * 0.28)
    for y in lines:
        canvas.shape(lambda shape, y=y: (shape.draw_line((x, y), (x + piece.width, y)),
                                         shape.finish(color=style.color, width=max(0.4, style.size * 0.06))))


def decode(text: str | None) -> bytes | None:
    try:
        return base64.b64decode(re.sub(r"\s+", "", text or ""), validate=True) or None
    except (binascii.Error, ValueError):
        return None


def image_bytes(el) -> bytes | None:
    """Embedded image data. Linked images are not fetched."""
    if el is None or el.get("transferEncoding", "base64") not in ("base64", ""):
        return None
    return decode(el.text)


def inset(rect, margins):
    top, right, bottom, left = margins
    return (rect[0] + left, rect[1] + top, rect[2] - right, rect[3] - bottom)


def regions(inner, caption, reserve: float, kind: str):
    """Split a field's content box into caption and widget areas."""
    x0, y0, x1, y1 = inner
    if caption is None:
        return None, inner
    if kind == "button":
        return inner, inner
    placement = caption.placement
    reserve = min(reserve, max(0.0, (x1 - x0) if placement in ("left", "right", "inline") else (y1 - y0)))
    if placement == "right":
        return (x1 - reserve, y0, x1, y1), (x0, y0, x1 - reserve, y1)
    if placement == "top":
        return (x0, y0, x1, y0 + reserve), (x0, y0 + reserve, x1, y1)
    if placement == "bottom":
        return (x0, y1 - reserve, x1, y1), (x0, y0, x1, y1 - reserve)
    return (x0, y0, x0 + reserve, y1), (x0 + reserve, y0, x1, y1)


class Painter:
    def __init__(self, doc: pdf.Document):
        self.doc = doc
        self.names: dict[int, str] = {}
        self.groups: dict[int, str] = {}
        self.radios: dict[str, list] = {}
        self.used: set[str] = set()

    def page(self, layout_page: Page):
        page = self.doc.new_page(width=layout_page.width, height=layout_page.height)
        canvas = Canvas(page)
        widgets: list[WidgetSpec] = []
        for box, x, y in layout_page.boxes:
            self.box(canvas, widgets, box, x, y)
        canvas.flush()
        for spec in widgets:
            xref = add_widget(self.doc, page, spec)
            if xref and spec.radio:
                self.radios.setdefault(spec.name, []).append((xref, pdf_name(spec.on or "Yes"), bool(spec.value), spec.label))

    def finish(self):
        """Join each radio group's buttons under one field, as PDF expects."""
        if not self.radios:
            return
        doc = self.doc
        catalog = doc.pdf_catalog()
        kind, form = doc.xref_get_key(catalog, "AcroForm")
        owner, key = (int(form.split()[0]), "Fields") if kind == "xref" else (catalog, "AcroForm/Fields")
        refs = [int(x) for x in re.findall(r"(\d+) 0 R", doc.xref_get_key(owner, key)[1])]
        for name, kids in self.radios.items():
            parent = doc.get_new_xref()
            value = next((on for _, on, chosen, _ in kids if chosen), "/Off")
            members = {xref for xref, *_ in kids}
            doc.update_object(parent, f"<</FT/Btn/Ff {RADIO}/T{pdf_text(name)}/TU{pdf_text(kids[0][3])}/V{value}"
                                      f"/Kids[{' '.join(f'{x} 0 R' for x in members)}]>>")
            for xref in members:
                # Field keys move to the parent. Setting a key to null would
                # leave it in the file, so the widget is written afresh.
                entries = []
                for item in doc.xref_get_keys(xref):
                    if item not in ("T", "TU", "FT", "Ff", "V", "Parent"):
                        kind, text = doc.xref_get_key(xref, item)
                        entries.append(f"/{item} {pdf_text(text) if kind == 'string' else text}")
                doc.update_object(xref, f"<<{' '.join(entries)} /Parent {parent} 0 R>>")
            refs = [r for r in refs if r not in members] + [parent]
        doc.xref_set_key(owner, key, "[" + " ".join(f"{r} 0 R" for r in refs) + "]")

    def box(self, canvas, widgets, box: Box, ox: float, oy: float):
        x, y = ox + box.x, oy + box.y
        node = box.node
        if node is None or node.presence == "invisible":
            return
        rect = (x, y, x + box.w, y + box.h)
        if node.tag in ("subform", "exclGroup", "area"):
            paint_outline(canvas, rect, outline_of(node.find("border")))
            for child in box.children:
                self.box(canvas, widgets, child, x, y)
        elif node.tag == "draw":
            self.draw(canvas, box, rect)
        elif node.tag == "field":
            self.field(canvas, widgets, box, rect)

    def draw(self, canvas: Canvas, box: Box, rect):
        el = box.node.el
        paint_outline(canvas, rect, outline_of(el.find("border")))
        content = box.node.content
        area = inset(rect, margin_of(el))
        if content is None or content.tag == "exData":
            block = box.info.get("block")
            if block is not None:
                paint_block(canvas, block, area, para_of(el).v)
        elif content.tag == "line":
            outline = outline_of(content)
            stroke = outline.edges[0] if outline else None
            if stroke is None or not stroke.visible:
                return
            x0, y0, x1, y1 = area
            if content.get("slope", "\\") == "/":
                a, b = (x0, y1), (x1, y0)
            else:
                a, b = (x0, y0), (x1, y1)
            canvas.shape(lambda shape: (shape.draw_line(a, b), shape.finish(
                color=stroke.color, width=stroke.thickness, dashes=dashes(stroke))))
        elif content.tag == "rectangle":
            paint_outline(canvas, area, outline_of(content, white=True))
        elif content.tag == "arc":
            paint_arc(canvas, area, content)
        elif content.tag == "image":
            data = image_bytes(content)
            if data:
                canvas.image(area, data, content.get("aspect", "fit") != "none")

    def field(self, canvas: Canvas, widgets, box: Box, rect):
        node, info = box.node, box.info
        el = node.el
        kind = info["kind"]
        paint_outline(canvas, rect, outline_of(el.find("border")))
        inner = inset(rect, margin_of(el))
        caption = info["caption"]
        cap_rect, widget = regions(inner, caption, info["reserve"], kind)
        if caption is not None:
            area = inset(cap_rect, caption.margin)
            paragraphs = paragraphs_for(caption.text, caption.rich, caption.font, caption.para)
            block = tx.layout(paragraphs, max(1.0, area[2] - area[0]) if kind != "button" else None)
            paint_block(canvas, block, area, caption.para.v)
        ui = ui_of(el)
        ui_border = outline_of(ui.find("border"), white=True) if ui is not None else None
        ui_margin = margin_of(ui) if ui is not None else (0.0,) * 4
        font, para = info["font"], info["para"]
        open_ = access_of(node) == "open" and kind in EDITABLE
        if kind == "checkButton":
            self.check(canvas, widgets, node, ui, widget, para, open_)
            return
        if kind in ("button", "barcode", "signature"):
            if kind != "button":
                paint_outline(canvas, widget, ui_border)
            return
        paint_outline(canvas, widget, ui_border)
        content = inset(widget, ui_margin)
        cells = comb_cells(node)
        if cells and ui_border is not None and ui_border.edges[3].visible:
            self.comb_lines(canvas, widget, cells, ui_border)
        if kind == "imageEdit":
            data = image_bytes(node.content) if node.content is not None else decode(node.value)
            if data:
                canvas.image(content, data, True)
            return
        if open_:
            widgets.append(self.widget_spec(node, kind, content, font, para, cells))
            return
        shown = display(node)
        if not shown:
            return
        if cells:
            self.comb_text(canvas, shown, content, cells, font, para)
        else:
            width = content[2] - content[0] if multiline(node) else None
            paint_block(canvas, tx.layout(tx.plain(shown, font, para), width), content, para.v)

    def comb_lines(self, canvas: Canvas, rect, cells: int, outline: Outline):
        edge = outline.edges[3]
        x0, y0, x1, y1 = rect
        step = (x1 - x0) / cells

        def draw(shape):
            for i in range(1, cells):
                x = x0 + i * step
                shape.draw_line((x, y0), (x, y1))
            shape.finish(color=edge.color, width=edge.thickness, dashes=dashes(edge))
        canvas.shape(draw)

    def comb_text(self, canvas: Canvas, value: str, rect, cells: int, font: Font, para):
        x0, y0, x1, y1 = rect
        step = (x1 - x0) / cells
        chars = list(value.replace("\n", " "))[:cells]
        start = {"right": cells - len(chars), "center": (cells - len(chars)) // 2}.get(para.h, 0)
        face = tx.face_of(font)
        height = face.ascent * font.size + face.descent * font.size
        top = y0 if para.v == "top" else y1 - height if para.v == "bottom" else y0 + (y1 - y0 - height) / 2
        baseline = top + face.ascent * font.size
        for i, ch in enumerate(chars):
            if ch.strip():
                cx = x0 + (start + i) * step + (step - face.width(ch, font.size)) / 2
                canvas.text(cx, baseline, ch, font)

    def check(self, canvas, widgets, node, ui, widget, para, open_):
        size = min(tx_size(ui), widget[2] - widget[0], widget[3] - widget[1]) if ui is not None else 10.0
        size = max(size, 1.0)
        x0, y0, x1, y1 = widget
        x = x0 if para.h == "left" else x1 - size if para.h == "right" else x0 + (x1 - x0 - size) / 2
        y = y0 if para.v == "top" else y1 - size if para.v == "bottom" else y0 + (y1 - y0 - size) / 2
        box = (x, y, x + size, y + size)
        round_ = ui is not None and ui.get("shape") == "round"
        border = ui.find("border") if ui is not None else None
        # Without a border of their own, check boxes get a plain one.
        outline = outline_of(border, white=True) if border is not None else Outline((Stroke(),) * 4)
        paint_outline(canvas, box, outline, oval=round_)
        if open_:
            widgets.append(self.widget_spec(node, "checkButton", box, None, para, 0))
            return
        if check_state(node):
            mark(canvas, box, ui.get("mark") if ui is not None else None, round_)

    def name(self, node) -> str:
        if node.data is not None and id(node.data) in self.names:
            return self.names[id(node.data)]
        # A period would split the name into a field hierarchy, so use slashes.
        name = "/".join(re.sub(r"\[0\]$", "", p) for p in node.path.split(".") if p) or "field"
        base, n = name, 1
        while name in self.used:
            n += 1
            name = f"{base}#{n}"
        self.used.add(name)
        if node.data is not None:
            self.names[id(node.data)] = name
        return name

    def widget_spec(self, node, kind, rect, font, para, cells) -> WidgetSpec:
        radio = node.parent is not None and node.parent.tag == "exclGroup"
        target = node.parent if radio else node
        if radio:
            name = self.groups.get(id(target)) or self.groups.setdefault(id(target), self.name(target))
        else:
            name = self.name(node)
        flags, max_length, choices = 0, 0, ()
        ui = ui_of(node.el)
        if kind in TEXT_KINDS:
            if multiline(node):
                flags |= pdf.PDF_TX_FIELD_IS_MULTILINE
            if kind == "passwordEdit":
                flags |= pdf.PDF_TX_FIELD_IS_PASSWORD
            if cells:
                flags |= pdf.PDF_TX_FIELD_IS_COMB
                max_length = cells
            text = node.el.find("value/text")
            if not cells and text is not None and text.get("maxChars"):
                max_length = max(0, int(text.get("maxChars", "0") or 0))
            value = node.value or ""
        elif kind == "choiceList":
            # Choices are their labels: XFA's saved codes mean nothing outside
            # the form, and viewers show a choice field's value as it is.
            if node.items:
                choices = tuple(label for _, label in node.items)
            else:
                lists = node.el.findall("items")
                shown = next((items for items in lists if items.get("save") != "1"), lists[0] if lists else None)
                choices = tuple(c.text or "" for c in shown) if shown is not None else ()
            if ui is not None and ui.get("open") == "multiSelect":
                flags |= pdf.PDF_CH_FIELD_IS_MULTI_SELECT
            if ui is not None and ui.get("textEntry") == "1":
                flags |= pdf.PDF_CH_FIELD_IS_EDIT
            value = display(node)
        else:
            value = check_state(node)
        return WidgetSpec(tuple(rect), kind, name, label_of(target), value, font or Font(), para.h,
                          flags, max_length, choices, on_value(node.el), radio)


def tx_size(ui) -> float:
    return measure(ui.get("size"), 10.0) or 10.0


def mark(canvas: Canvas, box, kind: str | None, round_: bool):
    x0, y0, x1, y1 = box
    s = x1 - x0
    kind = kind or ("circle" if round_ else "check")
    pad = s * 0.2
    r = pdf.Rect(x0 + pad, y0 + pad, x1 - pad, y1 - pad)
    width = max(0.6, s * 0.08)
    if kind == "cross":
        canvas.shape(lambda shape: (shape.draw_line(r.tl, r.br), shape.draw_line(r.tr, r.bl),
                                    shape.finish(color=(0, 0, 0), width=width, lineCap=1)))
    elif kind == "circle":
        canvas.shape(lambda shape: (shape.draw_oval(r), shape.finish(color=None, fill=(0, 0, 0))))
    elif kind == "square":
        canvas.shape(lambda shape: (shape.draw_rect(r), shape.finish(color=None, fill=(0, 0, 0))))
    elif kind == "diamond":
        points = [(r.x0 + r.width / 2, r.y0), (r.x1, r.y0 + r.height / 2), (r.x0 + r.width / 2, r.y1), (r.x0, r.y0 + r.height / 2)]
        canvas.shape(lambda shape: (shape.draw_polyline(points), shape.finish(color=None, fill=(0, 0, 0), closePath=True)))
    elif kind == "star":
        cx, cy, outer, inner_r = r.x0 + r.width / 2, r.y0 + r.height / 2, r.width / 2, r.width / 5
        points = [(cx + (outer if i % 2 == 0 else inner_r) * math.sin(i * math.pi / 5),
                   cy - (outer if i % 2 == 0 else inner_r) * math.cos(i * math.pi / 5)) for i in range(10)]
        canvas.shape(lambda shape: (shape.draw_polyline(points), shape.finish(color=None, fill=(0, 0, 0), closePath=True)))
    else:
        points = [(r.x0, r.y0 + r.height * 0.55), (r.x0 + r.width * 0.38, r.y1), (r.x1, r.y0)]
        canvas.shape(lambda shape: (shape.draw_polyline(points), shape.finish(
            color=(0, 0, 0), width=width * 1.3, lineCap=1, lineJoin=1, closePath=False)))


def label_of(node) -> str:
    """A readable name for the fill dialog: tooltip, spoken text, caption, or the field's path."""
    assist = node.el.find("assist")
    for tag in ("toolTip", "speak"):
        el = assist.find(tag) if assist is not None else None
        if el is not None and (el.text or "").strip():
            return " ".join(el.text.split())
    caption = node.el.find("caption/value")
    if caption is not None:
        text = " ".join("".join(caption.itertext()).split())
        if text:
            return text
    names = []
    current = node
    while current is not None and len(names) < 2:
        if current.name and not GENERIC.fullmatch(current.name) and current.tag != "pageSet":
            names.append(current.name.replace("_", " ").strip())
        current = current.parent
    return " / ".join(reversed(names)) or node.name or "Field"


def add_widget(doc: pdf.Document, page: pdf.Page, spec: WidgetSpec):
    widget = pdf.Widget()
    widget.rect = pdf.Rect(spec.rect)
    if widget.rect.is_empty:
        return
    widget.field_name = spec.name
    widget.field_label = spec.label
    widget.border_width = 0
    widget.border_color = None
    widget.fill_color = None
    face = tx.face_of(spec.font)
    base = getattr(face.font, "name", "") or ""
    widget.text_font = "Cour" if "Courier" in base or "Mono" in base else "TiRo" if "Times" in base or "Serif" in base else "Helv"
    widget.text_fontsize = spec.font.size if spec.kind != "checkButton" else 0
    widget.text_color = spec.font.color
    if spec.kind in TEXT_KINDS:
        widget.field_type = pdf.PDF_WIDGET_TYPE_TEXT
        widget.field_value = str(spec.value)
        if spec.max_length:
            widget.text_maxlen = spec.max_length
    elif spec.kind == "choiceList":
        multi = spec.flags & pdf.PDF_CH_FIELD_IS_MULTI_SELECT
        widget.field_type = pdf.PDF_WIDGET_TYPE_LISTBOX if multi else pdf.PDF_WIDGET_TYPE_COMBOBOX
        widget.choice_values = list(spec.choices)
        widget.field_value = str(spec.value)
    else:
        # Radio groups are made from check boxes: PyMuPDF cannot create a
        # selected radio button. Each gets the XFA on-value as its state name.
        widget.field_type = pdf.PDF_WIDGET_TYPE_CHECKBOX
        widget.field_value = bool(spec.value) and not spec.radio
    widget.field_flags = spec.flags
    try:
        annot = page.add_widget(widget)
    except Exception:
        return
    if spec.radio:
        on = pdf_name(spec.on or "Yes")
        _, states = doc.xref_get_key(annot.xref, "AP/N")
        doc.xref_set_key(annot.xref, "AP/N", states.replace("/Yes", on))
        doc.xref_set_key(annot.xref, "AS", on if spec.value else "/Off")
    quadding = {"center": 1, "right": 2, "radix": 2}.get(spec.align, 0)
    if quadding and spec.kind in TEXT_KINDS:
        doc.xref_set_key(annot.xref, "Q", str(quadding))
        loaded = page.load_widget(annot.xref)
        if loaded is not None:
            loaded.update()
    return annot.xref


def pdf_text(value: str) -> str:
    return "<FEFF" + value.encode("utf-16-be").hex().upper() + ">"


def pdf_name(value: str) -> str:
    """A PDF name object for any text."""
    return "/" + "".join(c if "!" <= c <= "~" and c not in "()<>[]{}/%#" else
                         "".join(f"#{b:02X}" for b in c.encode()) for c in value)
