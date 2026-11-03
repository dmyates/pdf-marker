"""Reading features, annotations, highlights, form scripts and XFA handling."""
from io import BytesIO

import pymupdf as pdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

from pdfmarker.document import Document, highlight


def test_text_links_search_outline_attachments_and_properties(reader):
    doc = Document(reader)
    assert doc.text(0).text().startswith("Alpha beta gamma\nSecond line of text")
    links = doc.links(0)
    assert [(l.kind, l.target) for l in links if l.kind == "uri"] == [
        ("uri", "https://example.com/"), ("uri", "javascript:alert(1)")]
    assert any(l.kind == "page" and l.dest_page == 1 and l.description == "Go to page 2" for l in links)
    assert any(l.kind == "file" and l.target == "/bin/sh" for l in links)
    hits = [(p, r) for p in range(2) for r in doc.search(p, "NEEDLE")]
    assert [p for p, _ in hits] == [0, 1]
    assert 380 < hits[0][1][1] < 400
    outline = doc.outline()
    assert [(e.level, e.title, e.page) for e in outline] == [(1, "Start", 0), (2, "Detail", 0), (1, "Second", 1)]
    assert outline[1].y == pytest.approx(380, abs=1)
    attachment, = doc.attachments()
    assert (attachment.name, attachment.size, attachment.description) == ("notes.txt", 13, "Notes")
    assert doc.attachment_bytes(attachment) == b"attached data"
    properties = dict(doc.properties())
    assert properties["Title"] == "Reader sample" and properties["Author"] == "Tester"
    assert properties["Pages"] == "2" and properties["Encryption"] == "None"


def test_page_text_carets_words_lines_and_rects(reader):
    text = Document(reader).text(0)
    first = text.lines[0]
    x0, y0, x1, y1 = first.rect
    middle = (y0 + y1) / 2
    assert text.caret(x0 - 5, middle) == 0
    assert text.caret(x1 + 40, middle) == first.end
    assert text.caret(0, 0) == 0 and text.caret(0, 1000) == len(text)
    assert text.text(*text.word(7)) == "beta"
    assert text.text(*text.line_range(first.end + 2)) == "Second line of text"
    assert len(text.rects(0, text.lines[1].end)) == 2
    assert text.hit(x0 + 2, middle) and not text.hit(x0, 300)


@pytest.mark.parametrize("rotation", [0, 90, 270])
def test_rotated_text_and_highlights_use_display_coordinates(tmp_path, rotation):
    path = tmp_path / "rotated.pdf"
    with pdf.open() as source:
        page = source.new_page(width=500, height=650)
        page.insert_text((60, 100), "Highlight this phrase", fontsize=14)
        page.set_rotation(rotation)
        source.save(path)
    doc = Document(path)
    width, height = doc.pages[0]
    rects = doc.search(0, "this phrase")
    assert rects and all(0 <= r[0] < r[2] <= width and 0 <= r[1] < r[3] <= height for r in rects)
    text = doc.text(0)
    line = text.lines[0]
    assert all(r[0] <= c[0] and c[2] <= r[2] + 1 and r[1] <= c[1] + 1 for c in text.chars[line.start:line.end] for r in [line.rect])
    doc.add(highlight(0, rects))
    with pdf.open(stream=doc.pdf_bytes(), filetype="pdf") as saved:
        page = saved[0]
        annot, = page.annots()
        assert annot.type[1] == "Highlight"
        assert "this phrase" in page.get_textbox(annot.rect)


def test_annotation_edit_delete_and_history(reader):
    doc = Document(reader)
    note, = doc.page_annotations(0)
    assert (note.kind, note.label, note.author, note.content) == ("Text", "Note", "Reviewer", "Original note")
    doc.edit_annotation(note, "Changed note")
    assert doc.dirty and doc.annotation_content(note) == "Changed note"
    with pdf.open(stream=doc.pdf_bytes(), filetype="pdf") as saved:
        page = saved[0]
        assert [a.info["content"] for a in page.annots()] == ["Changed note"]
    doc.delete_annotation(note)
    assert doc.page_annotations(0) == []
    with pdf.open(stream=doc.pdf_bytes(), filetype="pdf") as saved:
        page = saved[0]
        assert not list(page.annots())
        assert "needle" in page.get_text()
    doc.undo()
    assert doc.annotation_content(note) == "Changed note"
    doc.undo()
    assert not doc.dirty and doc.form_bytes() is doc._source


def test_freetext_edit_regenerates_appearance(tmp_path):
    path = tmp_path / "freetext.pdf"
    with pdf.open() as source:
        page = source.new_page()
        page.add_freetext_annot(pdf.Rect(50, 100, 300, 140), "Old words")
        source.save(path)
    doc = Document(path)
    box, = doc.page_annotations(0)
    doc.edit_annotation(box, "New words")
    with pdf.open(stream=doc.pdf_bytes(), filetype="pdf") as saved:
        page = saved[0]
        pix = page.get_pixmap()
        annot, = page.annots()
        assert annot.info["content"] == "New words"
    assert pix.samples != Document(path).render(0, 1).samples


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


def test_hybrid_xfa_fills_acroform_and_drops_stale_xfa(sample, tmp_path):
    writer = PdfWriter(clone_from=sample)
    writer.root_object["/AcroForm"][NameObject("/XFA")] = TextStringObject("<xdp:xdp/>")
    path = tmp_path / "hybrid.pdf"
    writer.write(path)
    doc = Document(path)
    assert doc.xfa and not doc.notice
    doc.fill(next(f for f in doc.fields if f.name == "name"), "Filled")
    saved = PdfReader(BytesIO(doc.pdf_bytes()))
    assert "/XFA" not in saved.trailer["/Root"]["/AcroForm"]
    assert saved.get_fields()["name"]["/V"] == "Filled"
    assert ("Form", "XFA form with 4 fields") in doc.properties()


def test_dynamic_xfa_opens_with_notice(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(300, 300)
    writer.root_object[NameObject("/AcroForm")] = DictionaryObject({
        NameObject("/Fields"): ArrayObject(), NameObject("/XFA"): TextStringObject("<xdp:xdp/>")})
    path = tmp_path / "dynamic.pdf"
    writer.write(path)
    doc = Document(path)
    assert doc.xfa and "dynamic XFA" in doc.notice and doc.render(0, 1)
