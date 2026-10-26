from datetime import datetime
from io import BytesIO

import pymupdf as pdf
from pypdf import PdfReader
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject, DecodedStreamObject
import pytest

from pdfmarker.document import Document, Mark, PasswordRequired


def test_export_all_tools_and_interactive_fields(sample):
    original = sample.read_bytes()
    doc = Document(sample)
    fields = {f.name: f for f in doc.fields}
    doc.fill(fields["name"], "David Smith")
    doc.fill(fields["agree"], "Yes")
    doc.fill(fields["choice"], "Two")
    doc.fill(fields["notes"], "Page two value")
    doc.add(Mark("text", 0, (40, 250, 300, 300), text="Arbitrary text", size=14))
    doc.add(Mark("draw", 1, (80, 300, 200, 350), points=((80, 350), (120, 300), (160, 345), (200, 315))))
    doc.add(Mark("tick", 0, (350, 140, 368, 158)))
    doc.add(Mark("cross", 1, (350, 140, 368, 158)))
    output = doc.export(now=datetime(2026, 10, 25, 14, 30, 22))
    assert output.name == "application-signed-20261025-143022.PDF"
    assert sample.read_bytes() == original
    assert not doc.dirty
    reader = PdfReader(output)
    fields = reader.get_fields()
    for name, value in [("name", "David Smith"), ("agree", "/Yes"), ("choice", "Two"), ("notes", "Page two value")]:
        assert fields[name]["/V"] == value
    for page in reader.pages:
        for ref in page["/Annots"]:
            widget = ref.get_object()
            assert widget["/V"] == fields[widget["/T"]]["/V"]
            appearance = widget["/AP"]["/N"].get_object()
            assert appearance
    with pdf.open(output) as saved:
        assert len(saved) == 2
        assert "Arbitrary text" in saved[0].get_text()
        assert saved[0].get_drawings()
        assert len(saved[1].get_drawings()) >= 2
    doc.undo()
    assert doc.dirty
    doc.redo()
    assert not doc.dirty
    assert doc.export(now=datetime(2026, 10, 25, 14, 30, 22)).name.endswith("-1.PDF")


def test_history_preserves_fields_and_marks(sample):
    doc = Document(sample)
    field = doc.fields[0]
    mark = Mark("tick", 0, (10, 20, 25, 35))
    doc.fill(field, "New")
    doc.add(mark)
    doc.update(0, mark.moved(10, 20))
    doc.delete(0)
    for _ in range(4):
        doc.undo()
    assert not doc.marks and not doc.values and not doc.dirty
    for _ in range(4):
        doc.redo()
    assert not doc.marks and doc.values[field.name] == "New"
    doc.undo()
    assert doc.marks[0].rect == (20, 40, 35, 55)
    doc.fill(field, "Different")
    assert not doc.can_redo


def test_clearing_field_round_trips(sample):
    doc = Document(sample)
    field = doc.fields[0]
    doc.fill(field, "")
    reader = PdfReader(BytesIO(doc.pdf_bytes()))
    assert reader.get_fields()[field.name]["/V"] == ""
    with pdf.open(stream=doc.pdf_bytes(), filetype="pdf") as saved:
        assert "Original" not in saved[0].get_text()


def test_text_overflow_does_not_record_edit(sample):
    doc = Document(sample)
    with pytest.raises(ValueError, match="does not fit"):
        doc.add(Mark("text", 0, (10, 20, 20, 25), text="Far too much text", size=20))
    assert not doc.can_undo and not doc.dirty


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_and_cropped_pages(tmp_path, rotation):
    path = tmp_path / "rotated.pdf"
    with pdf.open() as source:
        page = source.new_page(width=500, height=650)
        page.insert_text((110, 150), "Original")
        page.set_cropbox(pdf.Rect(50, 60, 450, 600))
        page.set_rotation(rotation)
        source.save(path)
        before = page.get_pixmap().samples
    doc = Document(path)
    assert doc.render(0, 1).samples == before
    doc.add(Mark("text", 0, (30, 30, 200, 65), text="Added here"))
    doc.add(Mark("tick", 0, (220, 100, 240, 120)))
    with pdf.open(stream=doc.pdf_bytes(), filetype="pdf") as result:
        text = result[0].search_for("Added here")[0] * result[0].rotation_matrix
        assert abs(text.x0 - 30) < 2
        assert 28 <= text.y0 < 45


def test_password_pdf(tmp_path):
    path = tmp_path / "secret.pdf"
    with pdf.open() as source:
        source.new_page()
        source.save(path, encryption=pdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    with pytest.raises(PasswordRequired):
        Document(path)
    assert Document(path, "secret").pages


def test_radio_group_parent_and_sibling_appearances(tmp_path):
    path = tmp_path / "radio.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(400, 500)
    kids = ArrayObject()
    group = DictionaryObject({NameObject('/FT'): NameObject('/Btn'),
                              NameObject('/T'): TextStringObject('preference'),
                              NameObject('/Ff'): NumberObject(1 << 15),
                              NameObject('/V'): NameObject('/Off'), NameObject('/Kids'): kids})
    parent = writer._add_object(group)
    for i, choice in enumerate(['Email', 'Post']):
        states = DictionaryObject()
        for state in ['Off', choice]:
            stream = DecodedStreamObject()
            stream.set_data(b'0 0 20 20 re S' if state == 'Off' else b'0 0 20 20 re f')
            stream[NameObject('/BBox')] = ArrayObject(map(NumberObject, [0, 0, 20, 20]))
            states[NameObject('/' + state)] = writer._add_object(stream)
        widget = DictionaryObject({NameObject('/Type'): NameObject('/Annot'),
            NameObject('/Subtype'): NameObject('/Widget'), NameObject('/Parent'): parent,
            NameObject('/Rect'): ArrayObject(map(NumberObject, [40, 100 + 50*i, 60, 120 + 50*i])),
            NameObject('/AS'): NameObject('/Off'),
            NameObject('/AP'): DictionaryObject({NameObject('/N'): states})})
        kids.append(writer._add_object(widget))
    page[NameObject('/Annots')] = kids
    writer.root_object[NameObject('/AcroForm')] = DictionaryObject({NameObject('/Fields'): ArrayObject([parent])})
    writer.write(path)
    doc = Document(path)
    email, post = doc.fields
    doc.fill(email, email.on_value)
    doc.fill(post, post.on_value)
    saved = PdfReader(BytesIO(doc.pdf_bytes()))
    field = saved.get_fields()['preference']
    assert field['/V'] == '/Post'
    widgets = [ref.get_object() for ref in saved.pages[0]['/Annots']]
    assert [w['/AS'] for w in widgets] == ['/Off', '/Post']
    assert all(w['/Parent']['/V'] == '/Post' for w in widgets)


def test_listbox_and_repeated_field_across_pages(tmp_path):
    path = tmp_path / 'repeated.pdf'
    with pdf.open() as source:
        for _ in range(2):
            page = source.new_page()
            widget = pdf.Widget()
            widget.field_type = pdf.PDF_WIDGET_TYPE_TEXT
            widget.field_name = 'repeated'
            widget.rect = pdf.Rect(30, 30, 250, 60)
            page.add_widget(widget)
        widget = pdf.Widget()
        widget.field_type = pdf.PDF_WIDGET_TYPE_LISTBOX
        widget.field_flags = 1 << 21
        widget.field_name = 'list'
        widget.choice_values = ['Red', 'Green', 'Blue']
        widget.rect = pdf.Rect(30, 100, 250, 170)
        page.add_widget(widget)
        source.save(path)
    doc = Document(path)
    doc.fill(doc.fields[0], 'Shared across pages')
    doc.fill(doc.fields[-1], ['Red', 'Blue'])
    result = PdfReader(BytesIO(doc.pdf_bytes()))
    assert result.get_fields()['list']['/V'] == ['Red', 'Blue']
    for page in result.pages:
        assert page['/Annots'][0].get_object()['/V'] == 'Shared across pages'
    doc.fill(doc.fields[-1], [])
    assert PdfReader(BytesIO(doc.pdf_bytes())).get_fields()['list']['/V'] == []


def test_group_insert_validates_before_changing_history(sample):
    doc=Document(sample)
    good=Mark('tick',0,(10,20,30,40))
    with pytest.raises(ValueError,match='Unknown mark'):
        doc.add_many([good,Mark('invalid',0,(40,20,60,40))])
    assert not doc.marks and not doc.can_undo
    doc.add_many([good,good.moved(40,0)])
    assert len(doc.marks)==2
    doc.undo()
    assert not doc.marks
    doc.redo()
    assert len(doc.marks)==2
