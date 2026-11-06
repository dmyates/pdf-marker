"""Geometry: sizing containers, positioned and flowed layout, and pagination.

Every container becomes a `Box` positioned relative to its parent. Flowing
content is laid out as one continuous column, then divided between content
areas and pages, honouring explicit breaks and keeping atomic boxes whole.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from . import text as tx
from .model import (Font, Node, Para, caption_of, font_of, integer, margin_of, measure, occurrences, para_of,
                    ui_of)
from .values import display

EPS = 0.01
MAX_PAGES = 2000
CONTAINERS = ("subform", "area", "exclGroup", "field", "draw")
STOCK = {
    "letter": (612, 792), "legal": (612, 1008), "tabloid": (792, 1224), "ledger": (792, 1224),
    "executive": (522, 756), "a3": (841.89, 1190.55), "a4": (595.28, 841.89), "a5": (419.53, 595.28),
    "b4": (708.66, 1000.63), "b5": (498.9, 708.66),
}


@dataclass(eq=False)
class Box:
    node: Node | None
    x: float
    y: float
    w: float
    h: float
    children: list[Box] = field(default_factory=list)
    margins: tuple = (0.0, 0.0, 0.0, 0.0)
    flowed: bool = False
    split: bool = False      # Growable flowed containers may continue in the next content area.
    breaks: bool = False     # A descendant in the flow asks for a break.
    before: tuple | None = None
    after: tuple | None = None
    info: dict = field(default_factory=dict)


@dataclass
class PageArea:
    node: Node
    width: float
    height: float
    areas: list[tuple[float, float, float, float]]
    master: list[Box]
    limit: int


@dataclass
class Page:
    width: float
    height: float
    boxes: list[tuple[Box, float, float]]


def limits(el):
    return (measure(el.get("minW"), 0.0) or 0.0, measure(el.get("maxW"), 0.0) or 0.0,
            measure(el.get("minH"), 0.0) or 0.0, measure(el.get("maxH"), 0.0) or 0.0)


def clamp(value: float, low: float, high: float) -> float:
    value = max(value, low)
    return min(value, high) if high > 0 else value


def anchor(el, w: float, h: float) -> tuple[float, float]:
    x, y = measure(el.get("x")), measure(el.get("y"))
    kind = el.get("anchorType", "topLeft")
    if kind.endswith("Center"):
        x -= w / 2
    elif kind.endswith("Right"):
        x -= w
    if kind.startswith("middle"):
        y -= h / 2
    elif kind.startswith("bottom"):
        y -= h
    return x, y


def target_name(value: str) -> str:
    value = value.strip().split()[0] if value.strip() else ""
    value = value.lstrip("#").split(".")[-1]
    return re.sub(r"\[[^\]]*\]$", "", value)


def breaks_of(el) -> tuple[tuple | None, tuple | None]:
    before = after = None
    old = el.find("break")
    if old is not None:
        kind = old.get("before", "auto")
        if kind in ("pageArea", "contentArea", "pageEven", "pageOdd"):
            before = ("contentArea" if kind == "contentArea" else "pageArea", target_name(old.get("beforeTarget", "")))
        kind = old.get("after", "auto")
        if kind in ("pageArea", "contentArea", "pageEven", "pageOdd"):
            after = ("contentArea" if kind == "contentArea" else "pageArea", target_name(old.get("afterTarget", "")))
    for node in el.findall("breakBefore"):
        if node.get("targetType", "auto") in ("pageArea", "contentArea"):
            before = (node.get("targetType"), target_name(node.get("target", "")))
    for node in el.findall("breakAfter"):
        if node.get("targetType", "auto") in ("pageArea", "contentArea"):
            after = (node.get("targetType"), target_name(node.get("target", "")))
    return before, after


def paragraphs_for(value: str | None, content, style: Font, para: Para) -> list[tx.Paragraph]:
    if content is not None and content.tag == "exData":
        body = content.find("body") if content.find("body") is not None else content.find("html")
        if body is not None:
            return tx.rich(body, style, para)
        return tx.plain("".join(content.itertext()), style, para)
    if not value:
        return []
    return tx.plain(value, style, para)


def kind_of(node: Node) -> str:
    ui = ui_of(node.el)
    return ui.tag if ui is not None else "textEdit"


def multiline(node: Node) -> bool:
    ui = ui_of(node.el)
    return ui is not None and ui.tag == "textEdit" and ui.get("multiLine") == "1"


def comb_cells(node: Node) -> int:
    ui = ui_of(node.el)
    comb = ui.find("comb") if ui is not None and ui.tag == "textEdit" else None
    return max(1, integer(comb.get("numberOfCells"), 0)) if comb is not None else 0


class Layout:
    def __init__(self, root: Node):
        self.root = root

    # Sizes and placement ---------------------------------------------------

    def box(self, node: Node, avail: float | None = None, force_w=None, force_h=None) -> Box:
        if node.tag == "draw":
            return self.draw(node, force_w, force_h)
        if node.tag == "field":
            return self.field(node, force_w, force_h)
        return self.container(node, avail, force_w, force_h)

    def container(self, node: Node, avail, force_w=None, force_h=None) -> Box:
        el = node.el
        kind = el.get("layout", "position") if node.tag in ("subform", "exclGroup") else "position"
        w = force_w if force_w is not None else measure(el.get("w"), None)
        h = force_h if force_h is not None else measure(el.get("h"), None)
        min_w, max_w, min_h, max_h = limits(el)
        top, right, bottom, left = margin_of(el)
        kids = [c for c in node.children if c.visible and c.tag in CONTAINERS]
        width = w if w is not None else avail
        inner = None if width is None else max(0.0, width - left - right)
        children: list[Box] = []
        if kind == "position":
            for child in kids:
                b = self.box(child)
                x, y = anchor(child.el, b.w, b.h)
                b.x, b.y = left + x, top + y
                children.append(b)
        elif kind == "tb":
            y = top
            for child in kids:
                b = self.box(child, inner)
                b.x, b.y = left, y
                y += b.h
                children.append(b)
        elif kind == "table":
            children = self.table(node, kids, left, top)
        else:  # lr-tb, rl-tb, and rows outside a table
            children = self.rows(kids, inner, left, top, kind.startswith("rl"))
        content_w = max((b.x + b.w for b in children), default=left) + right
        content_h = max((b.y + b.h for b in children), default=top) + bottom
        box = Box(node, 0.0, 0.0, w if w is not None else clamp(content_w, min_w, max_w),
                  h if h is not None else clamp(content_h, min_h, max_h), children, (top, right, bottom, left))
        box.before, box.after = breaks_of(el)
        box.flowed = kind != "position"
        keep = el.find("keep")
        intact = keep.get("intact", "none") if keep is not None else "none"
        box.split = box.flowed and h is None and intact == "none"
        box.breaks = box.flowed and any(c.before or c.after or c.breaks for c in children)
        return box

    def rows(self, kids, inner, left, top, rtl) -> list[Box]:
        out, x, y, row_h = [], 0.0, top, 0.0
        for child in kids:
            b = self.box(child, inner)
            if x > 0 and inner is not None and x + b.w > inner + EPS:
                x, y, row_h = 0.0, y + row_h, 0.0
            b.x = left + (inner - x - b.w if rtl and inner is not None else x)
            b.y = y
            x += b.w
            row_h = max(row_h, b.h)
            out.append(b)
        return out

    def table(self, node: Node, kids, left, top) -> list[Box]:
        widths = [measure(v, -1.0) for v in (node.el.get("columnWidths") or "").split()]
        rows = []
        for child in kids:
            if child.tag == "subform" and child.el.get("layout") in ("row", "rl-row"):
                rows.append((child, [c for c in child.children if c.visible and c.tag in CONTAINERS]))
            else:
                rows.append((child, None))
        natural = {i: 0.0 for i, v in enumerate(widths) if v < 0}
        for row, cells in rows:
            col = 0
            for cell in cells or ():
                span = integer(cell.el.get("colSpan"), 1)
                if span == 1 and col in natural:
                    natural[col] = max(natural[col], self.box(cell).w)
                col += max(span, 1)
        widths = [natural.get(i, v) for i, v in enumerate(widths)]
        out, y = [], top
        for row, cells in rows:
            if cells is None:
                b = self.box(row, sum(widths) or None)
                b.x, b.y = left, y
                y += b.h
                out.append(b)
                continue
            col, x, boxes = 0, 0.0, []
            for cell in cells:
                span = integer(cell.el.get("colSpan"), 1)
                span = len(widths) - col if span < 0 else max(span, 1)
                cw = sum(widths[col:col + span]) if col < len(widths) else self.box(cell).w
                col += span
                boxes.append((cell, cw))
            fixed = measure(row.el.get("h"), None)
            row_h = fixed if fixed is not None else max((self.box(c, cw, force_w=cw).h for c, cw in boxes), default=0.0)
            cell_boxes = []
            for cell, cw in boxes:
                b = self.box(cell, cw, force_w=cw, force_h=row_h)
                b.x, b.y = x, 0.0
                x += cw
                cell_boxes.append(b)
            rb = Box(row, left, y, x, row_h, cell_boxes)
            rb.before, rb.after = breaks_of(row.el)
            y += row_h
            out.append(rb)
        return out

    def draw(self, node: Node, force_w=None, force_h=None) -> Box:
        el = node.el
        w = force_w if force_w is not None else measure(el.get("w"), None)
        h = force_h if force_h is not None else measure(el.get("h"), None)
        min_w, max_w, min_h, max_h = limits(el)
        top, right, bottom, left = margin_of(el)
        box = Box(node, 0.0, 0.0, 0.0, 0.0)
        content = node.content
        if content is None or content.tag == "exData":
            paragraphs = paragraphs_for(node.value, content, font_of(el), para_of(el))
            if paragraphs:
                width = w - left - right if w is not None else (max_w - left - right if max_w else None)
                block = tx.layout(paragraphs, width)
                box.info["block"] = block
                if w is None:
                    w = clamp(block.width + left + right, min_w, max_w)
                if h is None:
                    h = clamp(block.height + top + bottom, min_h, max_h)
        box.w = w if w is not None else clamp(left + right, min_w, max_w)
        box.h = h if h is not None else clamp(top + bottom, min_h, max_h)
        return box

    def field(self, node: Node, force_w=None, force_h=None) -> Box:
        el = node.el
        font, para = font_of(el), para_of(el)
        top, right, bottom, left = margin_of(el)
        w = force_w if force_w is not None else measure(el.get("w"), None)
        h = force_h if force_h is not None else measure(el.get("h"), None)
        min_w, max_w, min_h, max_h = limits(el)
        kind = kind_of(node)
        caption = caption_of(el, font, para)
        cap_block, reserve = None, 0.0
        if caption:
            cap_block = tx.layout(paragraphs_for(caption.text, caption.rich, caption.font, caption.para), None)
            ct, cr, cb, cl = caption.margin
            if caption.reserve is not None:
                reserve = caption.reserve
            elif caption.placement in ("top", "bottom"):
                reserve = cap_block.height + ct + cb
            else:
                reserve = cap_block.width + cl + cr
        side = caption is not None and caption.placement not in ("top", "bottom") and kind != "button"
        stacked = caption is not None and caption.placement in ("top", "bottom") and kind != "button"
        ui = ui_of(el)
        block = None
        if kind == "checkButton":
            size = measure(ui.get("size"), 10.0) if ui is not None else 10.0
            content_w = content_h = size
        elif kind in ("button", "barcode", "signature", "imageEdit"):
            content_w = content_h = 0.0
        else:
            text = display(node)
            width = None
            if w is not None and multiline(node):
                width = w - left - right - (reserve if side else 0.0)
            block = tx.layout(tx.plain(text, font, para), width) if text else None
            content_w = block.width if block else 0.0
            content_h = block.height if block else tx.empty_line_height(font, para)
        if w is None:
            w = left + right + content_w + (reserve if side else 0.0)
            if stacked or kind == "button":
                w = max(w, left + right + (cap_block.width if cap_block else 0.0))
            w = clamp(w, min_w, max_w)
        if h is None:
            cap_h = cap_block.height if cap_block else 0.0
            if stacked:
                h = top + bottom + reserve + content_h
            else:
                h = top + bottom + max(content_h, cap_h)
            h = clamp(h, min_h, max_h)
        box = Box(node, 0.0, 0.0, w, h)
        box.info.update(kind=kind, font=font, para=para, caption=caption, caption_block=cap_block,
                        reserve=reserve, block=block)
        return box

    # Pages -------------------------------------------------------------------

    def page_areas(self) -> tuple[Node | None, list[PageArea]]:
        page_set = next((c for c in self.root.children if c.tag == "pageSet"), None)
        result = []

        def collect(node):
            for child in node.children:
                if child.tag == "pageSet":
                    collect(child)
                elif child.tag == "pageArea":
                    result.append(self.page_area(child))
        if page_set is not None:
            collect(page_set)
        return page_set, result

    def page_area(self, node: Node) -> PageArea:
        medium = node.find("medium")
        stock = STOCK.get((medium.get("stock") if medium is not None else "") or "letter", STOCK["letter"])
        short = measure(medium.get("short"), None) if medium is not None else None
        long = measure(medium.get("long"), None) if medium is not None else None
        short, long = short or stock[0], long or stock[1]
        landscape = medium is not None and medium.get("orientation") == "landscape"
        width, height = (long, short) if landscape else (short, long)
        areas = []
        for child in node.children:
            if child.tag == "contentArea" and child.visible:
                areas.append((measure(child.el.get("x")), measure(child.el.get("y")),
                              measure(child.el.get("w"), width), measure(child.el.get("h"), height)))
        master = []
        for child in node.children:
            if child.tag in CONTAINERS and child.visible:
                b = self.box(child)
                b.x, b.y = anchor(child.el, b.w, b.h)
                master.append(b)
        limit = occurrences(node.el)[1] if node.find("occur") is not None else -1
        return PageArea(node, width, height, areas, master, limit)

    def pages(self) -> list[Page]:
        page_set, areas = self.page_areas()
        if not any(area.areas for area in areas):
            # Without content areas, the form is its master pages, plus any
            # flowing content placed on the first page as in a default page.
            if areas:
                pages = [Page(a.width, a.height, [(b, 0.0, 0.0) for b in a.master]) for a in areas]
                if any(c.visible and c.tag in CONTAINERS for c in self.root.children):
                    pages[0].boxes.append((self.box(self.root, areas[0].width), 0.0, 0.0))
                return pages
            areas = [PageArea(self.root, 612, 792, [(18, 18, 576, 756)], [], -1)]
        first = next(a for a in areas if a.areas)
        content = self.box(self.root, first.areas[0][2])
        relation = page_set.el.get("relation", "orderedOccurrence") if page_set is not None else "orderedOccurrence"
        pages: list[Page] = []
        used: dict[int, int] = {}
        remaining: Box | None = content
        area = self.choose(areas, relation, used, None, leading_break(content), 0)
        stalled = 0
        while remaining is not None and len(pages) < MAX_PAGES and stalled < 3:
            page = Page(area.width, area.height, [(b, 0.0, 0.0) for b in area.master])
            brk, placed = None, False
            for x, y, w, h in area.areas:
                head, remaining, brk = self.split(remaining, h, True)
                if head is not None:
                    page.boxes.append((head, x, y))
                    placed = True
                if remaining is None or (brk and brk[0] == "pageArea"):
                    break
            stalled = 0 if placed else stalled + 1
            pages.append(page)
            used[id(area)] = used.get(id(area), 0) + 1
            if remaining is not None:
                area = self.choose(areas, relation, used, area, brk, len(pages))
        return pages

    def choose(self, areas, relation, used, current, brk, index) -> PageArea:
        if brk and brk[0] == "pageArea" and brk[1]:
            for area in areas:
                if brk[1] in (area.node.name, area.node.el.get("id")):
                    return area
        if relation in ("simplexPaginated", "duplexPaginated"):
            position = "first" if index == 0 else "rest"
            parity = "odd" if index % 2 == 0 else "even"
            for wanted in (position, "any"):
                for area in areas:
                    if area.node.el.get("pagePosition", "any") == wanted and \
                            area.node.el.get("oddOrEven", "any") in ("any", parity) and area.areas:
                        return area
        if current is None:
            return next((a for a in areas if a.areas), areas[0])
        if current.areas and (current.limit < 0 or used.get(id(current), 0) < current.limit):
            return current
        later = areas[areas.index(current) + 1:]
        return next((a for a in later if a.areas), next((a for a in areas if a.areas), current))

    def split(self, box: Box, avail: float, at_top: bool):
        """Fit `box` into `avail` height: (part that fits, remainder, break that caused the cut)."""
        fits = box.h <= avail + EPS
        if fits and not box.breaks:
            return box, None, None
        forced = at_top and not fits and box.children and box.node is not None and box.node.tag in ("subform", "area")
        if not (box.split or box.breaks or forced):
            if fits or at_top:
                return box, None, None
            return None, box, None
        if not box.split and not fits and not at_top:
            return None, box, None
        top, right, bottom, left = box.margins
        limit = avail - bottom
        head: dict[int, Box] = {}
        tail: dict[int, Box] = {}
        rows = rows_of(box.children)
        brk = None
        cut_at = None  # (row index, tail fragment of a split child or None)
        for i, row in enumerate(rows):
            placed = bool(head) or not at_top
            first = row[0]
            if box.flowed and first.before and placed:
                cut_at, brk = (i, None), first.before
                break
            row_bottom = max(b.y + b.h for b in row)
            nested = box.flowed and len(row) == 1 and row[0].breaks
            if row_bottom <= limit + EPS and not nested:
                head.update((id(b), b) for b in row)
                after = next((b.after for b in row if b.after), None) if box.flowed else None
                if after and i + 1 < len(rows):
                    cut_at, brk = (i + 1, None), after
                    break
                continue
            if len(row) == 1 and (row[0].split or row[0].breaks or not placed):
                child = row[0]
                part, rest, child_brk = self.split(child, limit - child.y, at_top and not head)
                if part is not None:
                    head[id(child)] = part
                if rest is not None:
                    cut_at, brk = (i, rest), child_brk
                    break
                continue
            if not placed:
                head.update((id(b), b) for b in row)
                continue
            cut_at = (i, None)
            break
        if cut_at is None:
            return fragment(box, [head.get(id(b)) for b in box.children]), None, None
        index, rest = cut_at
        if rest is not None:
            child = rows[index][0]
            shift = child.y + child.h - (top + rest.h)
            tail[id(child)] = replace(rest, y=top)
            later = rows[index + 1:]
        else:
            later = rows[index:]
            shift = (min(b.y for b in later[0]) - top) if later else 0.0
        for row in later:
            for b in row:
                tail[id(b)] = replace(b, y=b.y - shift)
        head_box = fragment(box, [head.get(id(b)) for b in box.children]) if head else None
        tail_box = fragment(box, [tail.get(id(b)) for b in box.children])
        return head_box, tail_box, brk


def rows_of(children: list[Box]) -> list[list[Box]]:
    rows: list[list] = []
    for b in sorted(children, key=lambda b: (b.y, b.x)):
        if rows and b.y < rows[-1][1] - EPS:
            rows[-1][2].append(b)
            rows[-1][1] = max(rows[-1][1], b.y + b.h)
        else:
            rows.append([b.y, b.y + b.h, [b]])
    return [row[2] for row in rows]


def fragment(box: Box, children: list[Box | None]) -> Box:
    kept = [c for c in children if c is not None]
    if len(kept) == len(box.children) and all(a is b for a, b in zip(kept, box.children)):
        return box
    bottom = max((c.y + c.h for c in kept), default=box.margins[0]) + box.margins[2]
    result = replace(box, children=kept, h=bottom)
    result.breaks = box.flowed and any(c.before or c.after or c.breaks for c in kept)
    return result


def leading_break(box: Box) -> tuple | None:
    while box is not None:
        if box.before:
            return box.before
        if not box.flowed or not box.children:
            return None
        box = min(box.children, key=lambda b: (b.y, b.x))
    return None
