"""PDF operations and history, independent of the GUI.

All overlay coordinates are PDF points in the displayed page orientation.
Forms remain interactive; overlays become ordinary vector/text PDF content.
"""
from __future__ import annotations

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


class PasswordRequired(ValueError):
    pass


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
            self._source = source.tobytes(encryption=pdf.PDF_ENCRYPT_NONE)
        reader = PdfReader(BytesIO(self._source))
        form = reader.trailer["/Root"].get("/AcroForm")
        form = form.get_object() if form is not None else None
        if form and "/XFA" in form.get_object():
            raise ValueError("XFA forms are not supported. Use an AcroForm or a flat PDF.")
        self.pages: list[tuple[float, float]] = []
        self.fields: list[Field] = []
        with pdf.open(stream=self._source, filetype="pdf") as source:
            for page in source:
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
        self.calculation_order = self._calculation_order(form)
        self.marks: list[Mark] = []
        self.values: dict[str, object] = {}
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._saved = self._state()
        self._cached: bytes | None = self._source
        self._form_cache = ({}, self._source)

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
        return deepcopy((self.marks, self.values))

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
            if mark.kind not in {"text", "draw", "tick", "cross"}:
                raise ValueError("Unknown mark type")
            if not 0 <= mark.page < len(self.pages):
                raise ValueError("This page is not in the document.")
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

    def undo(self):
        if self._undo:
            self._redo.append(self._state())
            self.marks, self.values = self._undo.pop()
            self._cached = None

    def redo(self):
        if self._redo:
            self._undo.append(self._state())
            self.marks, self.values = self._redo.pop()
            self._cached = None

    @staticmethod
    def _paint(page, mark: Mark):
        rect = pdf.Rect(mark.rect)
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

    def form_bytes(self) -> bytes:
        """Page backgrounds with form values, without editable overlay marks."""
        if self.values == self._form_cache[0]:
            return self._form_cache[1]
        content = self._source
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
            buf = BytesIO()
            writer.write(buf)
            content = buf.getvalue()
        self._form_cache = (deepcopy(self.values), content)
        return content

    def pdf_bytes(self) -> bytes:
        if self._cached is not None:
            return self._cached
        content = self.form_bytes()
        with pdf.open(stream=content, filetype="pdf") as document:
            for number in sorted({m.page for m in self.marks}):
                page = document[number]
                # Paint in display coordinates, then rotate the vector overlay
                # back into the page's original coordinate space. Preserve crop
                # boxes, rotation, existing content, and interactive widgets.
                with pdf.open() as overlay:
                    layer = overlay.new_page(width=page.rect.width, height=page.rect.height)
                    for mark in self.marks:
                        if mark.page == number:
                            self._paint(layer, mark)
                    rotation = page.rotation
                    page.set_rotation(0)
                    page.show_pdf_page(page.rect, overlay, 0, rotate=rotation)
                    page.set_rotation(rotation)
            self._cached = document.tobytes(deflate=True, encryption=pdf.PDF_ENCRYPT_NONE)
        return self._cached

    def render(self, page: int, scale: float = 1.5, include_marks: bool = True):
        content = self.pdf_bytes() if include_marks else self.form_bytes()
        with pdf.open(stream=content, filetype="pdf") as document:
            return document[page].get_pixmap(matrix=pdf.Matrix(scale, scale), alpha=False)

    def export(self, directory: str | Path | None = None, now: datetime | None = None) -> Path:
        folder = Path(directory) if directory else self.path.parent
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        stem = f"{self.path.stem}-signed-{stamp}"
        content = self.pdf_bytes()  # Build before creating an output file.
        suffix = 0
        while True:
            extra = f"-{suffix}" if suffix else ""
            target = folder / f"{stem}{extra}{self.path.suffix}"
            try:
                out = target.open("xb")
            except FileExistsError:
                suffix += 1
                continue
            try:
                with out:
                    out.write(content)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            break
        self._saved = self._state()
        return target
