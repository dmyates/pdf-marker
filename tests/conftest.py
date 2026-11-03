import os
import tempfile

# Keep recent files, remembered positions and settings out of the user's home.
_home = tempfile.mkdtemp(prefix="pdf-marker-tests-")
for _name in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
    os.environ[_name] = os.path.join(_home, _name.lower())
# Under xvfb-run, never open test windows on the desktop's Wayland session.
if os.environ.get("DISPLAY"):
    os.environ["GDK_BACKEND"] = "x11"
    os.environ.pop("WAYLAND_DISPLAY", None)

import pymupdf as pdf
import pytest


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "application.PDF"
    with pdf.open() as doc:
        page = doc.new_page(width=500, height=650)
        page.insert_text((40, 40), "Application form", fontsize=20)
        for name, kind, rect, value in [
            ("name", pdf.PDF_WIDGET_TYPE_TEXT, (40, 80, 300, 110), "Original"),
            ("agree", pdf.PDF_WIDGET_TYPE_CHECKBOX, (40, 140, 60, 160), "Off"),
            ("choice", pdf.PDF_WIDGET_TYPE_COMBOBOX, (40, 180, 200, 210), "One"),
        ]:
            w = pdf.Widget()
            w.field_name, w.field_type, w.rect, w.field_value = name, kind, pdf.Rect(rect), value
            w.text_fontsize = 12
            if name == "choice":
                w.choice_values = ["One", "Two", "Three"]
            page.add_widget(w)
        page = doc.new_page(width=500, height=650)
        page.insert_text((40, 40), "Second page")
        w = pdf.Widget()
        w.field_name, w.field_type, w.rect = "notes", pdf.PDF_WIDGET_TYPE_TEXT, pdf.Rect(40, 80, 300, 120)
        page.add_widget(w)
        doc.save(path)
    return path


@pytest.fixture
def reader(tmp_path):
    """Two pages with text, links, a note, an outline, an attachment and metadata."""
    path = tmp_path / "reader.pdf"
    with pdf.open() as doc:
        page = doc.new_page(width=500, height=650)
        page.insert_text((40, 60), "Alpha beta gamma", fontsize=14)
        page.insert_text((40, 90), "Second line of text", fontsize=14)
        page.insert_text((40, 400), "Find the needle here", fontsize=12)
        doc.new_page(width=500, height=650).insert_text((40, 60), "Page two needle", fontsize=14)
        page = doc[0]
        page.insert_link({"kind": pdf.LINK_URI, "from": pdf.Rect(40, 120, 200, 140), "uri": "https://example.com/"})
        page.insert_link({"kind": pdf.LINK_URI, "from": pdf.Rect(40, 150, 200, 170), "uri": "javascript:alert(1)"})
        page.insert_link({"kind": pdf.LINK_GOTO, "from": pdf.Rect(40, 180, 200, 200), "page": 1, "to": pdf.Point(0, 300)})
        page.insert_link({"kind": pdf.LINK_LAUNCH, "from": pdf.Rect(40, 210, 200, 230), "file": "/bin/sh"})
        note = page.add_text_annot((400, 60), "Original note")
        note.set_info(title="Reviewer")
        note.update()
        doc.set_toc([[1, "Start", 1], [2, "Detail", 1, {"kind": pdf.LINK_GOTO, "page": 0, "to": pdf.Point(0, 380)}],
                     [1, "Second", 2]])
        doc.embfile_add("notes.txt", b"attached data", filename="notes.txt", desc="Notes")
        doc.set_metadata({"title": "Reader sample", "author": "Tester"})
        doc.save(path)
    return path
