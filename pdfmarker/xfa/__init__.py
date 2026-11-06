"""Lay out dynamic XFA forms as ordinary PDF pages.

Dynamic XFA forms (made with Adobe LiveCycle Designer, used by SARS and many
government agencies) contain only a placeholder page. Their real content is an
XML template plus data, which Adobe Reader lays out when the file opens. This
package does that layout itself and produces a standard PDF: static content as
vector graphics and text, read-only values as text, and editable fields as
AcroForm fields. Scripts in the form are never run.
"""
from __future__ import annotations

from contextlib import suppress
from io import BytesIO

import pymupdf as pdf
from pypdf import PdfReader

from .layout import Layout
from .model import Merge, compensate, parse_xml, resolve_protos
from .paint import Painter


def xdp(reader: PdfReader):
    """The XFA packets of a PDF as {name: element}, or None."""
    form = reader.trailer["/Root"].get("/AcroForm")
    form = form.get_object() if form is not None else None
    xfa = form.get("/XFA") if form is not None else None
    if xfa is None:
        return None
    xfa = xfa.get_object()
    parts = [part.get_object().get_data() for part in (list(xfa)[1::2] if isinstance(xfa, list) else [xfa])]
    try:
        root = parse_xml(b"".join(parts))
        return {child.tag: child for child in root} if root.tag == "xdp" else {root.tag: root}
    except ValueError:
        if len(parts) < 2:
            raise
    # One malformed packet should not cost the others, so try them one by one.
    packets = {}
    for part in parts:
        with suppress(ValueError):
            root = parse_xml(part)
            packets.setdefault(root.tag, root)
    return packets


def is_dynamic(reader: PdfReader) -> bool:
    """XFA without AcroForm fields: its pages are only a placeholder."""
    root = reader.trailer["/Root"]
    form = root.get("/AcroForm")
    form = form.get_object() if form is not None else None
    if form is None or "/XFA" not in form:
        return False
    needs = root.get("/NeedsRendering")
    return bool(needs) or not (form.get("/Fields") or [])


def convert(source: bytes) -> bytes:
    """Lay out the XFA form in `source` and return a standard PDF."""
    reader = PdfReader(BytesIO(source))
    packets = xdp(reader)
    if not packets or "template" not in packets:
        raise ValueError("This PDF has no XFA template.")
    template = packets["template"]
    resolve_protos(template)
    root = Merge(packets.get("datasets")).form(template)
    compensate(root)
    pages = Layout(root).pages()
    with pdf.open() as doc:
        painter = Painter(doc)
        for page in pages:
            painter.page(page)
        painter.finish()
        with pdf.open(stream=source, filetype="pdf") as original:
            meta = {k: v for k, v in (original.metadata or {}).items() if k not in ("format", "encryption") and v}
        doc.set_metadata(meta)
        doc.subset_fonts()
        return doc.tobytes(garbage=3, deflate=True, clean=True)
