"""PDF operations and history, independent of the GUI.

All overlay coordinates are PDF points in the displayed page orientation.
Forms remain interactive; overlays become ordinary vector/text PDF content.
"""
from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path

import pymupdf as pdf
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

from . import scripts
from .pagetext import PageText

MARK_KINDS = {"text", "draw", "tick", "cross", "highlight"}
HIGHLIGHT = (1.0, .87, .2)


@dataclass(frozen=True)
class Mark:
    kind: str
    page: int
    rect: tuple[float, float, float, float]
    text: str = ""
    points: tuple[tuple[float, float], ...] = ()
    size: float = 12
    width: float = 1.5
    color: tuple[float, float, float] = (0, 0, 0)
    appearance: bytes = b""

    def moved(self, dx: float, dy: float) -> Mark:
        x0, y0, x1, y1 = self.rect
        return replace(self, rect=(x0 + dx, y0 + dy, x1 + dx, y1 + dy),
                       points=tuple((x + dx, y + dy) for x, y in self.points))

    @property
    def quads(self) -> tuple[tuple[float, float, float, float], ...]:
        """Highlight rectangles, stored as corner pairs in `points`."""
        return tuple((*self.points[i], *self.points[i + 1]) for i in range(0, len(self.points) - 1, 2))


def highlight(page: int, rects, color=HIGHLIGHT, text: str = "") -> Mark:
    rects = [tuple(r) for r in rects]
    box = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
    points = tuple(p for r in rects for p in ((r[0], r[1]), (r[2], r[3])))
    return Mark("highlight", page, box, text=text, points=points, color=color)


@dataclass(frozen=True)
class Field:
    page: int
    name: str
    label: str
    kind: int
    rect: tuple[float, float, float, float]
    value: object
    choices: tuple[tuple[str, str], ...]
    on_value: str | bool | None
    flags: int
    max_length: int
    format: str = ""
    keystroke: str = ""
    validate: str = ""
    calculate: str = ""


@dataclass(frozen=True)
class Link:
    page: int
    rect: tuple[float, float, float, float]
    kind: str  # uri, page, named, file
    target: str = ""
    dest_page: int = -1
    dest_y: float | None = None

    @property
    def description(self) -> str:
        if self.kind == "uri":
            return self.target
        if self.kind == "page":
            return f"Go to page {self.dest_page + 1}"
        if self.kind == "file":
            return f"Open {self.target}"
        return self.target


@dataclass(frozen=True)
class Annotation:
    page: int
    xref: int
    kind: str
    rect: tuple[float, float, float, float]
    author: str
    content: str
    modified: str

    @property
    def label(self) -> str:
        return ANNOTATION_LABELS.get(self.kind, self.kind)


ANNOTATION_LABELS = {
    "Text": "Note", "FreeText": "Text box", "Highlight": "Highlight", "Underline": "Underline",
    "StrikeOut": "Strikethrough", "Squiggly": "Squiggly underline", "Square": "Rectangle",
    "Circle": "Ellipse", "Line": "Line", "Polygon": "Polygon", "PolyLine": "Polyline",
    "Ink": "Drawing", "Stamp": "Stamp", "FileAttachment": "Attachment", "Caret": "Insertion",
    "Sound": "Sound", "Redact": "Redaction",
}


@dataclass(frozen=True)
class OutlineEntry:
    level: int
    title: str
    page: int
    y: float | None
    expanded: bool


@dataclass(frozen=True)
class Attachment:
    name: str
    size: int
    description: str
    page: int = -1  # -1: document-level embedded file
    key: int = 0    # embedded file index, or annotation xref


class PasswordRequired(ValueError):
    pass


def pdf_date(value: str) -> str:
    """Format a PDF date string (D:YYYYMMDDHHmmSS...) for display."""
    digits = value[2:] if value.startswith("D:") else value
    for size, fmt, out in ((14, "%Y%m%d%H%M%S", "%-d %b %Y, %H:%M"), (8, "%Y%m%d", "%-d %b %Y")):
        try:
            return datetime.strptime(digits[:size], fmt).strftime(out)
        except ValueError:
            continue
    return value


def _qualified_name(obj) -> str:
    parts = []
    while obj is not None:
        if "/T" in obj:
            parts.append(str(obj["/T"]))
        parent = obj.get("/Parent")
        obj = parent.get_object() if parent is not None else None
    return ".".join(reversed(parts))


class Document:
    def __init__(self, path: str | Path, password: str = ""):
        self.path = Path(path).resolve()
        with pdf.open(self.path) as source:
            if source.needs_pass and not source.authenticate(password):
                raise PasswordRequired("This PDF needs a password.")
            if not source.is_pdf or not source.page_count:
                raise ValueError("Choose a PDF with at least one page.")
            self.encryption = source.metadata.get("encryption") or ""
            self._source = source.tobytes(encryption=pdf.PDF_ENCRYPT_NONE)
        reader = PdfReader(BytesIO(self._source))
        root = reader.trailer["/Root"]
        form = root.get("/AcroForm")
        form = form.get_object() if form is not None else None
        self.xfa = bool(form and "/XFA" in form)
        self._doc = pdf.open(stream=self._source, filetype="pdf")
        self.pages: list[tuple[float, float]] = []
        self.fields: list[Field] = []
        self.annotations: list[Annotation] = []
        for page in self._doc:
            self.pages.append((page.rect.width, page.rect.height))
            for widget in page.widgets() or ():
                self.fields.append(Field(
                    page.number, widget.field_name, widget.field_label or widget.field_name,
                    widget.field_type, tuple(widget.rect * page.rotation_matrix), widget.field_value,
                    tuple((str(v[0]), str(v[1])) if isinstance(v, (list, tuple))
                          else (str(v), str(v)) for v in widget.choice_values or ()), widget.on_state(),
                    widget.field_flags, widget.text_maxlen or 0,
                    widget.script_format or "", widget.script_stroke or "",
                    widget.script_change or "", widget.script_calc or "",
                ))
            for annot in page.annots() or ():
                kind = annot.type[1]
                if kind in ("Popup", "Link", "Widget") or annot.flags & pdf.PDF_ANNOT_IS_HIDDEN:
                    continue
                info = annot.info
                self.annotations.append(Annotation(
                    page.number, annot.xref, kind, tuple((annot.rect * page.rotation_matrix).normalize()),
                    info.get("title", ""), info.get("content", ""),
                    pdf_date(info.get("modDate") or info.get("creationDate") or "")))
        self.notice = ""
        if self.xfa and not self.fields:
            self.notice = ("This is a dynamic XFA form. Only Adobe Acrobat or Reader can show its "
                           "interactive content; this app shows the PDF's fallback pages.")
        self.calculation_order = self._calculation_order(form)
        self.marks: list[Mark] = []
        self.values: dict[str, object] = {}
        self.annot_edits: dict[int, str | None] = {}
        self.saved_path: Path | None = None
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._saved = self._state()
        self._cached: bytes | None = self._source
        self._base_cache: tuple = ((), self._source)
        self._form_cache: tuple = (((), {}), self._source)
        self._views: dict[bool, tuple[bytes, pdf.Document]] = {}
        self._text: dict[int, PageText] = {}
        self._links: dict[int, list[Link]] = {}

    def close(self):
        for _, view in self._views.values():
            view.close()
        self._views.clear()
        self._doc.close()

    def _calculation_order(self, form) -> list[str]:
        names = []
        for ref in (form or {}).get("/CO", ()) or ():
            with suppress(Exception):
                names.append(_qualified_name(ref.get_object()))
        calculated = [f.name for f in self.fields if f.calculate]
        names = [n for n in names if n in calculated]
        names += [n for n in dict.fromkeys(calculated) if n not in names]
        return names

    def _state(self):
        return deepcopy((self.marks, self.values, self.annot_edits))

    def _restore(self, state):
        self.marks, self.values, self.annot_edits = state
        self._cached = None

    @property
    def dirty(self):
        return self._state() != self._saved

    @property
    def can_undo(self):
        return bool(self._undo)

    @property
    def can_redo(self):
        return bool(self._redo)

    def _change(self):
        self._undo.append(self._state())
        self._redo.clear()
        self._cached = None

    def add(self, mark: Mark):
        self.add_many([mark])

    def add_many(self, marks):
        """Validate the entire insertion before recording one history entry."""
        marks = list(marks)
        if not marks:
            return
        for mark in marks:
            if mark.kind not in MARK_KINDS:
                raise ValueError("Unknown mark type")
            if not 0 <= mark.page < len(self.pages):
                raise ValueError("This page is not in the document.")
            if mark.kind == "highlight" and (not mark.points or len(mark.points) % 2):
                raise ValueError("A highlight needs at least one rectangle.")
            if mark.kind == "text":
                with pdf.open() as trial:
                    page = trial.new_page(width=self.pages[mark.page][0], height=self.pages[mark.page][1])
                    self._paint(page, mark)
        self._change()
        self.marks.extend(marks)

    def update(self, index: int, mark: Mark):
        self.update_many({index: mark})

    def update_many(self, changes: dict[int, Mark]):
        changes = {i: mark for i, mark in changes.items() if self.marks[i] != mark}
        if not changes:
            return
        for mark in changes.values():
            if mark.kind == "text":
                with pdf.open() as trial:
                    page = trial.new_page(width=self.pages[mark.page][0], height=self.pages[mark.page][1])
                    self._paint(page, mark)
        self._change()
        for index, mark in changes.items():
            self.marks[index] = mark

    def delete(self, index: int):
        self.delete_many([index])

    def delete_many(self, indices):
        indices = set(indices)
        if not indices:
            return
        self._change()
        self.marks = [mark for i, mark in enumerate(self.marks) if i not in indices]

    # Form fields and their Acrobat built-in scripts.

    def value_of(self, field: Field, values=None):
        values = self.values if values is None else values
        return values.get(field.name, field.value)

    def _values_of(self, values):
        by_name = {}
        for field in self.fields:
            by_name.setdefault(field.name, field)

        def lookup(name):
            return ["" if (v := self.value_of(f, values)) in (None, False) else str(v)
                    for n, f in by_name.items() if n == name or n.startswith(name + ".")]
        return lookup

    def _recalculate(self, values):
        by_name = {f.name: f for f in self.fields}
        for name in self.calculation_order:
            field = by_name[name]
            result = scripts.calculate(field.calculate, self._values_of(values))
            if result is not None and result != str(self.value_of(field, values) or ""):
                values[name] = result
        return values

    def display_value(self, field: Field, value=None) -> str:
        value = self.value_of(field) if value is None else value
        text = "" if value is None else str(value)
        if field.format and field.kind in (pdf.PDF_WIDGET_TYPE_TEXT, pdf.PDF_WIDGET_TYPE_COMBOBOX):
            formatted = scripts.format_display(field.format, text)
            if formatted is not None:
                return formatted
        return text

    def fill(self, field: Field, value: object):
        if field.flags & 1:
            raise ValueError("This field is read-only.")
        if isinstance(value, str) and field.kind in (pdf.PDF_WIDGET_TYPE_TEXT, pdf.PDF_WIDGET_TYPE_COMBOBOX):
            value = scripts.commit_value(field.label, field.keystroke, field.validate, value)
        if value == self.value_of(field):
            return
        values = self._recalculate({**self.values, field.name: value})
        # Build a candidate first. A malformed form must not poison the current
        # document or discard a previous undo entry when its update fails.
        old_values, old_cached = self.values, self._cached
        self.values = values
        self._cached = None
        try:
            candidate = self.pdf_bytes()
        finally:
            self.values, self._cached = old_values, old_cached
        self._change()
        self.values = values
        self._cached = candidate

    # Existing annotations: edits are applied to the source on output.

    def annotation_content(self, annotation: Annotation) -> str | None:
        """Current content, or None once deleted."""
        return self.annot_edits.get(annotation.xref, annotation.content)

    def page_annotations(self, page: int) -> list[Annotation]:
        return [a for a in self.annotations if a.page == page and self.annotation_content(a) is not None]

    def edit_annotation(self, annotation: Annotation, content: str | None):
        if content == self.annotation_content(annotation):
            return
        self._change()
        if content == annotation.content:
            self.annot_edits.pop(annotation.xref, None)
        else:
            self.annot_edits[annotation.xref] = content

    def delete_annotation(self, annotation: Annotation):
        self.edit_annotation(annotation, None)

    def undo(self):
        if self._undo:
            self._redo.append(self._state())
            self._restore(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._state())
            self._restore(self._redo.pop())

    @staticmethod
    def _paint(page, mark: Mark):
        rect = pdf.Rect(mark.rect)
        if mark.kind == "highlight":
            return
        if mark.kind == "text":
            if mark.appearance:
                with pdf.open(stream=mark.appearance, filetype="pdf") as appearance:
                    page.show_pdf_page(rect, appearance, 0, keep_proportion=False)
                return
            html = '<div style="white-space:pre-wrap">' + escape(mark.text) + '</div>'
            rgb = ",".join(str(round(c * 255)) for c in mark.color)
            spare, scale = page.insert_htmlbox(
                rect, html, css=f"* {{margin:0; padding:0;}} body {{font-family:sans-serif; font-size:{mark.size}pt; color:rgb({rgb});}}",
                scale_low=1,
            )
            if spare < 0:
                raise ValueError("The text does not fit. Draw a larger text box or use a smaller text size.")
            return
        shape = page.new_shape()
        if mark.kind == "draw":
            if len(mark.points) == 1:
                shape.draw_circle(mark.points[0], mark.width / 2)
            else:
                shape.draw_polyline(mark.points)
        elif mark.kind == "tick":
            shape.draw_polyline([(rect.x0, rect.y0 + rect.height * .52),
                                 (rect.x0 + rect.width * .36, rect.y1), (rect.x1, rect.y0)])
        elif mark.kind == "cross":
            shape.draw_line(rect.top_left, rect.bottom_right)
            shape.draw_line(rect.top_right, rect.bottom_left)
        shape.finish(color=mark.color, fill=mark.color if len(mark.points) == 1 else None,
                     width=mark.width, lineCap=1, lineJoin=1, closePath=False)
        shape.commit()

    def _base_bytes(self) -> bytes:
        """Source with annotation edits applied."""
        key = tuple(sorted(self.annot_edits.items(), key=lambda item: item[0]))
        if self._base_cache[0] == key:
            return self._base_cache[1]
        content = self._source
        if key:
            by_xref = {a.xref: a for a in self.annotations}
            with pdf.open(stream=self._source, filetype="pdf") as document:
                for xref, value in key:
                    annotation = by_xref[xref]
                    page = document[annotation.page]
                    annot = page.load_annot(xref)
                    if value is None:
                        page.delete_annot(annot)
                    else:
                        annot.set_info(content=value, modDate=pdf.get_pdf_now())
                        if annotation.kind == "FreeText":
                            annot.update()
                content = document.tobytes(encryption=pdf.PDF_ENCRYPT_NONE)
        self._base_cache = (key, content)
        return content

    def form_bytes(self) -> bytes:
        """Page backgrounds with form values and annotation edits, without overlay marks."""
        key = (tuple(sorted(self.annot_edits.items(), key=lambda item: item[0])), self.values)
        if self._form_cache[0] == key:
            return self._form_cache[1]
        content = self._base_bytes()
        if self.values:
            writer = PdfWriter(clone_from=BytesIO(content))
            known = writer.get_fields() or {}
            missing = self.values.keys() - known.keys()
            if missing:
                raise ValueError("This PDF has malformed form fields: " + ", ".join(sorted(missing)))
            fields = {f.name: f for f in self.fields}
            values, raw = {}, {}
            for name, value in self.values.items():
                field = fields[name]
                if field.kind in (pdf.PDF_WIDGET_TYPE_CHECKBOX, pdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                    value = NameObject("/" + str(value).lstrip("/"))
                elif isinstance(value, str) and (shown := self.display_value(field, value)) != value:
                    raw[name] = value
                    value = shown
                values[name] = value
            writer.update_page_form_field_values(None, values, auto_regenerate=False)
            # Formatted appearances keep the typed value, as Acrobat does.
            for page in writer.pages:
                for ref in page.get("/Annots", ()) or ():
                    widget = ref.get_object()
                    name = _qualified_name(widget)
                    if widget.get("/Subtype") == "/Widget" and name in raw:
                        target = widget if "/T" in widget else widget["/Parent"].get_object()
                        target[NameObject("/V")] = TextStringObject(raw[name])
            if self.xfa:
                # Filled AcroForm values must win over stale XFA data in other viewers.
                form = writer.root_object["/AcroForm"].get_object()
                form.pop(NameObject("/XFA"), None)
                writer.root_object.pop(NameObject("/NeedsRendering"), None)
            buf = BytesIO()
            writer.write(buf)
            content = buf.getvalue()
        self._form_cache = (deepcopy(key), content)
        return content

    def pdf_bytes(self) -> bytes:
        if self._cached is not None:
            return self._cached
        content = self.form_bytes()
        with pdf.open(stream=content, filetype="pdf") as document:
            for number in sorted({m.page for m in self.marks}):
                page = document[number]
                marks = [m for m in self.marks if m.page == number]
                if any(m.kind != "highlight" for m in marks):
                    # Paint in display coordinates, then rotate the vector overlay
                    # back into the page's original coordinate space. Preserve crop
                    # boxes, rotation, existing content, and interactive widgets.
                    with pdf.open() as overlay:
                        layer = overlay.new_page(width=page.rect.width, height=page.rect.height)
                        for mark in marks:
                            self._paint(layer, mark)
                        rotation = page.rotation
                        page.set_rotation(0)
                        page.show_pdf_page(page.rect, overlay, 0, rotate=rotation)
                        page.set_rotation(rotation)
                for mark in marks:
                    if mark.kind == "highlight":
                        quads = [pdf.Rect(r) * page.derotation_matrix for r in mark.quads]
                        annot = page.add_highlight_annot(quads)
                        annot.set_colors(stroke=mark.color)
                        annot.update()
            self._cached = document.tobytes(deflate=True, encryption=pdf.PDF_ENCRYPT_NONE)
        return self._cached

    def view(self, include_marks: bool = False) -> pdf.Document:
        """An open document for the current content, reused until it changes."""
        content = self.pdf_bytes() if include_marks else self.form_bytes()
        current = self._views.get(include_marks)
        if current is None or current[0] is not content:
            if current:
                current[1].close()
            current = self._views[include_marks] = (content, pdf.open(stream=content, filetype="pdf"))
        return current[1]

    def render(self, page: int, scale: float = 1.5, include_marks: bool = True):
        return self.view(include_marks)[page].get_pixmap(matrix=pdf.Matrix(scale, scale), alpha=False)

    # Reading: text, links, outline, attachments, properties.

    def text(self, page: int) -> PageText:
        if page not in self._text:
            source = self._doc[page]
            flags = pdf.TEXT_PRESERVE_WHITESPACE | pdf.TEXT_MEDIABOX_CLIP
            self._text[page] = PageText.from_rawdict(source.get_text("rawdict", flags=flags), source.rotation_matrix)
        return self._text[page]

    def search(self, page: int, query: str) -> list[tuple[float, float, float, float]]:
        source = self._doc[page]
        return [tuple((r * source.rotation_matrix).normalize()) for r in source.search_for(query)]

    def links(self, page: int) -> list[Link]:
        if page in self._links:
            return self._links[page]
        result = []
        for link in self._doc[page].get_links():
            rect, kind = tuple(link["from"]), link.get("kind")
            if kind == pdf.LINK_URI and link.get("uri"):
                result.append(Link(page, rect, "uri", link["uri"]))
            elif kind in (pdf.LINK_GOTO, pdf.LINK_NAMED) and 0 <= link.get("page", -1) < len(self.pages):
                target = link["page"]
                to = link.get("to")
                y = to.y if to is not None and not self._doc[target].rotation else None
                result.append(Link(page, rect, "page", dest_page=target, dest_y=y))
            elif kind == pdf.LINK_NAMED:
                result.append(Link(page, rect, "named", link.get("name") or link.get("nameddest") or ""))
            elif kind in (pdf.LINK_GOTOR, pdf.LINK_LAUNCH) and link.get("file"):
                result.append(Link(page, rect, "file", link["file"], dest_page=max(0, link.get("page", 0))))
        self._links[page] = result
        return result

    def outline(self) -> list[OutlineEntry]:
        entries = []
        for item in self._doc.get_toc(simple=False):
            level, title, number = item[:3]
            dest = item[3] if len(item) > 3 else {}
            page = number - 1 if 0 < number <= len(self.pages) else -1
            to = dest.get("to") if isinstance(dest, dict) else None
            y = to.y if to is not None and page >= 0 and not self._doc[page].rotation else None
            entries.append(OutlineEntry(level, title.strip() or "Untitled", page, y,
                                        not (isinstance(dest, dict) and dest.get("collapse"))))
        return entries

    def attachments(self) -> list[Attachment]:
        items = []
        for i in range(self._doc.embfile_count()):
            info = self._doc.embfile_info(i)
            items.append(Attachment(info.get("ufilename") or info.get("filename") or info.get("name") or f"Attachment {i+1}",
                                    info.get("size") or info.get("length") or 0, info.get("description", ""), -1, i))
        for annotation in self.annotations:
            if annotation.kind == "FileAttachment":
                page = self._doc[annotation.page]
                info = page.load_annot(annotation.xref).file_info
                items.append(Attachment(info.get("filename") or "Attachment", info.get("size") or info.get("length") or 0,
                                        info.get("description", ""), annotation.page, annotation.xref))
        return items

    def attachment_bytes(self, attachment: Attachment) -> bytes:
        if attachment.page < 0:
            return self._doc.embfile_get(attachment.key)
        page = self._doc[attachment.page]
        return page.load_annot(attachment.key).get_file()

    def properties(self) -> list[tuple[str, str]]:
        meta = self._doc.metadata or {}
        try:
            size = self.path.stat().st_size
            size_text = f"{size/1048576:.1f} MB" if size >= 1048576 else f"{max(1, round(size/1024))} KB"
        except OSError:
            size_text = ""
        w, h = self.pages[0]
        page_size = f"{w/72*25.4:.0f} × {h/72*25.4:.0f} mm ({w/72:.2f} × {h/72:.2f} in)"
        for name, (pw, ph) in {"A4": (595, 842), "A3": (842, 1191), "A5": (420, 595), "Letter": (612, 792), "Legal": (612, 1008)}.items():
            if abs(min(w, h) - pw) < 2 and abs(max(w, h) - ph) < 2:
                page_size = f"{name}, {page_size}"
                break
        rows = [
            ("File", self.path.name), ("Folder", str(self.path.parent)), ("File size", size_text),
            ("Pages", str(len(self.pages))), ("Page size", page_size),
            ("Title", meta.get("title", "")), ("Author", meta.get("author", "")),
            ("Subject", meta.get("subject", "")), ("Keywords", meta.get("keywords", "")),
            ("Created", pdf_date(meta.get("creationDate", ""))), ("Modified", pdf_date(meta.get("modDate", ""))),
            ("Application", meta.get("creator", "")), ("PDF producer", meta.get("producer", "")),
            ("PDF version", meta.get("format", "").replace("PDF ", "")),
            ("Encryption", self.encryption or "None"),
            ("Form", ("XFA form" if self.xfa else "Interactive form") + f" with {len({f.name for f in self.fields})} fields" if self.fields or self.xfa else "None"),
        ]
        return [(k, v) for k, v in rows if v]

    def save(self, path: str | Path) -> Path:
        """Write atomically, replacing an existing file only once the new one is complete."""
        path = Path(path).expanduser().resolve()
        content = self.pdf_bytes()  # Build before touching the destination.
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                umask = os.umask(0)
                os.umask(umask)
                mode = 0o666 & ~umask
            os.chmod(temp, mode)
            os.replace(temp, path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temp)
            raise
        self.path = self.saved_path = path
        self._saved = self._state()
        return path
