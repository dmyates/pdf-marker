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
