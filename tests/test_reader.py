"""Form-script integration with PDF field values and appearances."""
from io import BytesIO

import pymupdf as pdf
import pytest
from pypdf import PdfReader

from pdfmarker.document import Document


def scripted_form(path):
    with pdf.open() as source:
        page = source.new_page(width=500, height=400)
        for i, (name, scripts) in enumerate([
            ("qty", dict(script_stroke='AFNumber_Keystroke(0, 0, 0, 0, "", true);',
                         script_change="AFRange_Validate(true, 0, true, 100);")),
            ("price", dict(script_stroke='AFNumber_Keystroke(2, 0, 0, 0, "$", true);',
                           script_format='AFNumber_Format(2, 0, 0, 0, "$", true);')),
            ("total", dict(script_calc='AFSimple_Calculate("PRD", new Array ("qty", "price"));',
                           script_format='AFNumber_Format(2, 0, 0, 0, "$", true);', field_flags=1)),
            ("date", dict(script_stroke='AFDate_KeystrokeEx("yyyy-mm-dd");',
                          script_format='AFDate_FormatEx("d mmm yyyy");')),
            ("custom", dict(script_calc='event.value = this.getField("qty").value * 2;')),
        ]):
            widget = pdf.Widget()
            widget.field_name, widget.field_type = name, pdf.PDF_WIDGET_TYPE_TEXT
            widget.rect = pdf.Rect(40, 30 + 50*i, 300, 60 + 50*i)
            widget.text_fontsize = 12
            for key, value in scripts.items():
                setattr(widget, key, value)
            page.add_widget(widget)
        source.save(path)
    return path


def test_form_scripts_calculate_format_and_validate(tmp_path):
    doc = Document(scripted_form(tmp_path / "scripts.pdf"))
    fields = {f.name: f for f in doc.fields}
    assert doc.calculation_order == ["total", "custom"]
    doc.fill(fields["qty"], "3")
    doc.fill(fields["price"], "$2.50")
    assert doc.values["price"] == "2.5" and doc.values["total"] == "7.5"
    assert "custom" not in doc.values  # Custom JavaScript is never run.
    with pytest.raises(ValueError, match="less than or equal to 100"):
        doc.fill(fields["qty"], "101")
    with pytest.raises(ValueError, match="Invalid date"):
        doc.fill(fields["date"], "2026-02-30")
    doc.fill(fields["date"], "2026-02-28")
    assert doc.display_value(fields["date"]) == "28 Feb 2026"
    assert doc.display_value(fields["total"]) == "$7.50"
    saved = PdfReader(BytesIO(doc.pdf_bytes()))
    values = saved.get_fields()
    assert values["total"]["/V"] == "7.5" and values["price"]["/V"] == "2.5"
    appearances = {w["/T"]: w["/AP"]["/N"].get_object().get_data() for w in (r.get_object() for r in saved.pages[0]["/Annots"])}
    assert b"$7.50" in appearances["total"] and b"28 Feb 2026" in appearances["date"]
    doc.undo()
    doc.undo()
    assert "total" not in doc.values or doc.values["total"] == "0"
