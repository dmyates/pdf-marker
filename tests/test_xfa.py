"""Dynamic XFA forms: merging template and data, layout, pages and fields."""
from io import BytesIO

import pymupdf as pdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject, NameObject

from pdfmarker.document import Document
from pdfmarker.xfa import convert
from pdfmarker.xfa.model import measure
from pdfmarker.xfa.values import format_date, format_number, format_text

PAGES = """
<pageSet>
  <pageArea name="Page1">
    <contentArea x="0.5in" y="0.5in" w="7.5in" h="{height}"/>
    <medium stock="letter" short="8.5in" long="11in"/>
    <draw name="Footer" x="0.5in" y="10.5in" w="7.5in" h="0.3in">
      <font typeface="Arial" size="8pt"/><value><text>Footer on every page</text></value>
    </draw>
  </pageArea>
  <pageArea name="Wide">
    <contentArea x="0.5in" y="0.5in" w="10in" h="7in"/>
    <medium stock="letter" short="8.5in" long="11in" orientation="landscape"/>
  </pageArea>
</pageSet>
"""


def xfa_pdf(path, body, data="", height="9.5in", lookups=""):
    """A dynamic XFA PDF whose only real page is the usual placeholder."""
    xdp = f"""<?xml version="1.0" encoding="UTF-8"?>
<xdp:xdp xmlns:xdp="http://ns.adobe.com/xdp/">
<template xmlns="http://www.xfa.org/schema/xfa-template/3.3/">
<subform name="form1" layout="tb" locale="en_ZA">
{PAGES.format(height=height)}
{body}
</subform>
</template>
<xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/"><xfa:data>{data}</xfa:data>{lookups}</xfa:datasets>
</xdp:xdp>"""
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(xdp.encode())
    writer.root_object[NameObject("/AcroForm")] = DictionaryObject({
        NameObject("/Fields"): ArrayObject(), NameObject("/XFA"): writer._add_object(stream)})
    writer.root_object[NameObject("/NeedsRendering")] = BooleanObject(True)
    writer.add_metadata({"/Title": "Synthetic XFA form"})
    writer.write(path)
    return path


def text(doc: Document) -> list[str]:
    return [doc.view()[i].get_text() for i in range(len(doc.pages))]


def field(name, x="0mm", y="0mm", w="60mm", extra='access="readOnly"', inner=""):
    ui = "" if "<ui>" in inner else "<ui><textEdit/></ui>"
    return f'<field name="{name}" x="{x}" y="{y}" w="{w}" h="7mm" {extra}>{ui}<font typeface="Arial" size="10pt"/>{inner}</field>'


def test_layout_merges_data_into_a_standard_pdf(tmp_path):
    body = f"""
    <subform name="Details" w="7.5in">
      <draw name="Heading" w="7.5in" h="10mm"><font typeface="Arial" size="14pt" weight="bold"/>
        <value><text>Personal details</text></value></draw>
      {field("Name", y="12mm", w="120mm", inner='<caption reserve="25mm"><value><text>Full name</text></value></caption>')}
      {field("Code", y="22mm", w="40mm", inner='<ui><textEdit><border><edge/></border><comb numberOfCells="4"/></textEdit></ui>')}
    </subform>"""
    path = xfa_pdf(tmp_path / "simple.pdf", body, "<form1><Details><Name>Jane Doe</Name><Code>1234</Code></Details></form1>")
    doc = Document(path)
    assert doc.xfa and doc.xfa_layout and "laid out" in doc.notice
    assert len(doc.pages) == 1 and doc.pages[0] == pytest.approx((612, 792))
    page = text(doc)[0]
    for expected in ("Personal details", "Full name", "Jane Doe", "Footer on every page"):
        assert expected in page
    digits = [w for w in doc.view()[0].get_text("words") if w[4] in "1234"]
    assert [w[4] for w in digits] == list("1234")  # One comb cell per digit, 40mm / 4 apart.
    assert digits[1][0] - digits[0][0] == pytest.approx(10 * 72 / 25.4, abs=0.5)
    assert not doc.fields  # Read-only values are page text, not form fields.
    assert doc.search(0, "Jane Doe")
    assert ("Form", "Dynamic XFA form, laid out by PDF Marker") in doc.properties()
    assert doc.suggested_path.name == "simple (standard PDF).pdf"
    saved = PdfReader(BytesIO(doc.pdf_bytes()))
    assert "/XFA" not in (saved.trailer["/Root"].get("/AcroForm") or {})
    assert saved.metadata.title == "Synthetic XFA form"


def test_repeating_subforms_follow_the_data_and_flow_onto_new_pages(tmp_path):
    body = f"""
    <subform name="Items" layout="tb" w="7.5in">
      <subform name="Item" w="7.5in" h="20mm">
        <occur min="1" max="-1"/>
        {field("Label", w="100mm")}
      </subform>
    </subform>"""
    rows = "".join(f"<Item><Label>Row {i:02d}</Label></Item>" for i in range(40))
    doc = Document(xfa_pdf(tmp_path / "rows.pdf", body, f"<form1><Items>{rows}</Items></form1>"))
    pages = text(doc)
    # 9.5in holds 12 rows of 20mm, so 40 rows need 4 pages, each with its footer.
    assert len(pages) == 4
    assert all("Footer on every page" in page for page in pages)
    assert "Row 00" in pages[0] and "Row 11" in pages[0] and "Row 12" in pages[1] and "Row 39" in pages[3]
    assert sum(page.count("Row ") for page in pages) == 40


def test_breaks_choose_page_areas_and_intact_sections_move_whole(tmp_path):
    body = """
    <subform name="First" w="7.5in" h="8in"><draw name="A" w="50mm" h="8mm"><value><text>First section</text></value></draw></subform>
    <subform name="Kept" w="7.5in" layout="tb"><keep intact="contentArea"/>
      <subform name="Top" w="7.5in" h="1in"><draw name="B" w="50mm" h="8mm"><value><text>Kept start</text></value></draw></subform>
      <subform name="Bottom" w="7.5in" h="1in"><draw name="C" w="50mm" h="8mm"><value><text>Kept end</text></value></draw></subform>
    </subform>
    <subform name="Landscape" w="9in" h="1in"><breakBefore targetType="pageArea" target="Wide"/>
      <draw name="D" w="50mm" h="8mm"><value><text>Wide page</text></value></draw></subform>
    <subform name="Hidden" presence="hidden" w="1in" h="1in"><breakBefore targetType="pageArea" target="Page1"/></subform>"""
    doc = Document(xfa_pdf(tmp_path / "breaks.pdf", body))
    pages = text(doc)
    assert len(pages) == 3
    assert "First section" in pages[0] and "Kept start" not in pages[0]
    assert "Kept start" in pages[1] and "Kept end" in pages[1]
    assert "Wide page" in pages[2] and doc.pages[2] == pytest.approx((792, 612))


def test_editable_fields_become_form_fields_that_fill_and_save(tmp_path):
    body = f"""
    <subform name="Answers" w="7.5in">
      {field("Comment", w="100mm", extra='', inner='<assist><toolTip>Your comment</toolTip></assist>'
             '<para hAlign="right"/><value><text maxChars="20"/></value>')}
      <field name="Agree" x="0mm" y="10mm" w="40mm" h="7mm"><ui><checkButton size="10pt"/></ui>
        <items><text>1</text><text>0</text></items>
        <caption placement="right"><value><text>I agree</text></value></caption></field>
      <exclGroup name="Size" x="0mm" y="20mm">
        <field name="Small" w="20mm" h="7mm"><ui><checkButton shape="round"/></ui><items><text>S</text></items></field>
        <field name="Large" x="25mm" w="20mm" h="7mm"><ui><checkButton shape="round"/></ui><items><text>L</text></items></field>
      </exclGroup>
      <field name="Colour" x="0mm" y="30mm" w="50mm" h="7mm"><ui><choiceList/></ui>
        <items><text>Red</text><text>Blue</text></items><items save="1"><text>R</text><text>B</text></items></field>
    </subform>"""
    data = "<form1><Answers><Comment>Hello</Comment><Agree>1</Agree><Size>L</Size><Colour>B</Colour></Answers></form1>"
    doc = Document(xfa_pdf(tmp_path / "editable.pdf", body, data))
    by_label = {f.label: f for f in doc.fields}
    comment = by_label["Your comment"]
    assert comment.kind == pdf.PDF_WIDGET_TYPE_TEXT and comment.value == "Hello" and comment.max_length == 20
    assert by_label["I agree"].kind == pdf.PDF_WIDGET_TYPE_CHECKBOX and by_label["I agree"].value not in ("Off", False)
    radios = [f for f in doc.fields if f.kind == pdf.PDF_WIDGET_TYPE_RADIOBUTTON]
    assert sorted(f.on_value for f in radios) == ["L", "S"] and len({f.name for f in radios}) == 1
    colour = next(f for f in doc.fields if f.kind == pdf.PDF_WIDGET_TYPE_COMBOBOX)
    assert ("Blue", "Blue") in colour.choices and colour.value == "Blue"
    doc.fill(comment, "Changed")
    doc.fill(next(f for f in radios if f.on_value == "S"), "S")
    saved_path = doc.save(tmp_path / "filled.pdf")
    saved = PdfReader(saved_path)
    fields = saved.get_fields()
    assert fields[comment.name]["/V"] == "Changed" and fields[radios[0].name]["/V"] == "/S"
    assert len(fields[radios[0].name]["/Kids"]) == 2  # One radio field, two buttons.
    assert "/XFA" not in saved.trailer["/Root"]["/AcroForm"]
    states = {w.on_state(): doc_widget_state(w) for w in pdf.open(saved_path)[0].widgets()
              if w.field_type == pdf.PDF_WIDGET_TYPE_RADIOBUTTON}
    assert states == {"S": "S", "L": "Off"}


def doc_widget_state(widget) -> str:
    return widget.parent.parent.xref_get_key(widget.xref, "AS")[1].lstrip("/")


def test_scripts_never_run_but_hidden_data_is_shown(tmp_path):
    body = f"""
    <subform name="Person" w="7.5in">
      {field("BirthHidden", x="20mm", w="40mm", extra='access="readOnly" presence="hidden"',
             inner='<bind match="dataRef" ref="$.DOB"/>')}
      {field("Birth", x="20mm", w="40mm", inner='<bind match="none"/><event activity="initialize">'
             '<script contentType="application/x-javascript">this.rawValue = "SCRIPT RAN";</script></event>')}
      <subform name="Postal" y="10mm" w="7.5in" presence="hidden"><bind match="none"/>
        {field("Box", inner='<bind match="dataRef" ref="$.PostBox"/>')}
      </subform>
      <subform name="Empty" y="20mm" w="7.5in" presence="hidden"><bind match="none"/>
        <draw name="Note" w="50mm" h="8mm"><value><text>Only for companies</text></value></draw>
        {field("Company", y="8mm", inner='<bind match="dataRef" ref="$.Company"/>')}
      </subform>
    </subform>"""
    data = "<form1><Person><DOB>19920214</DOB><PostBox>PO Box 3</PostBox><Company/></Person></form1>"
    page = text(Document(xfa_pdf(tmp_path / "scripts.pdf", body, data)))[0]
    assert "SCRIPT RAN" not in page
    assert page.count("19920214") == 1  # Shown once, in the visible field.
    assert "PO Box 3" in page
    assert "Only for companies" not in page


def test_rich_text_borders_and_shapes_are_drawn(tmp_path):
    body = """
    <subform name="Art" w="7.5in">
      <draw name="Rich" w="120mm" h="20mm"><font typeface="Arial" size="10pt"/><value><exData contentType="text/html">
        <body xmlns="http://www.w3.org/1999/xhtml"><p>Plain <span style="font-weight:bold">bold</span>
        <span style="xfa-spacerun:yes">  </span>text</p><p style="color:#ff0000">Second paragraph</p></body>
      </exData></value></draw>
      <draw name="Box" y="25mm" w="30mm" h="10mm"><value><rectangle><edge thickness="1pt"/><fill><color value="200,220,255"/></fill></rectangle></value></draw>
      <draw name="Rule" y="40mm" w="100mm" h="0mm"><value><line><edge thickness="2pt"><color value="0,0,255"/></edge></line></value></draw>
      <draw name="Ring" y="45mm" w="10mm" h="10mm"><value><arc/></value></draw>
      <subform name="Framed" y="60mm" w="50mm" h="10mm"><border><edge stroke="dashed"/><corner radius="2mm"/></border></subform>
    </subform>"""
    doc = Document(xfa_pdf(tmp_path / "art.pdf", body))
    page = doc.view()[0]
    words = page.get_text()
    assert "Plain bold" in words and "Second paragraph" in words
    spans = [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]]
    assert any(s["text"].strip() == "bold" and s["flags"] & pdf.TEXT_FONT_BOLD for s in spans)
    assert any(s["text"].startswith("Second") and s["color"] == 0xFF0000 for s in spans)
    drawings = page.get_drawings()
    assert any(d.get("fill") and tuple(round(c, 2) for c in d["fill"]) == (0.78, 0.86, 1.0) for d in drawings)
    assert any(d.get("color") == (0.0, 0.0, 1.0) and d.get("width") == pytest.approx(2) for d in drawings)
    assert any(d.get("dashes", "[] 0") not in ("[] 0", None) for d in drawings)


def test_choice_lists_take_items_from_lookup_data(tmp_path):
    items = '<ui><choiceList/></ui><bindItems ref="!Lists.Country[*]" labelRef="$" valueRef="code"/>'
    body = f"""
    <subform name="Address" w="7.5in">
      {field("Country", w="60mm", inner=items)}
      {field("Destination", y="10mm", w="60mm", extra="", inner=items)}
    </subform>"""
    lookups = '<Lists><Country code="ZA">South Africa</Country><Country code="CA">Canada</Country></Lists>'
    data = "<form1><Address><Country>ZA</Country><Destination>CA</Destination></Address></form1>"
    doc = Document(xfa_pdf(tmp_path / "lookup.pdf", body, data, lookups=lookups))
    assert "South Africa" in text(doc)[0] and "ZA" not in text(doc)[0]
    destination = next(f for f in doc.fields if f.kind == pdf.PDF_WIDGET_TYPE_COMBOBOX)
    assert destination.value == "Canada" and [label for _, label in destination.choices] == ["South Africa", "Canada"]


def test_tables_place_cells_in_columns(tmp_path):
    body = """
    <subform name="Table" layout="table" columnWidths="40mm 60mm" w="100mm">
      <subform name="Row" layout="row"><occur max="-1"/>
        <draw name="Key" h="8mm"><value><text>Key</text></value></draw>
        <field name="Value" h="8mm" access="readOnly"><ui><textEdit/></ui></field>
      </subform>
    </subform>"""
    data = "<form1><Table><Row><Value>One</Value></Row><Row><Value>Two</Value></Row></Table></form1>"
    page = Document(xfa_pdf(tmp_path / "table.pdf", body, data)).view()[0]
    one, two = page.search_for("One")[0], page.search_for("Two")[0]
    key = page.search_for("Key")[0]
    assert one.x0 == pytest.approx(36 + 40 * 72 / 25.4, abs=1) and key.x0 == pytest.approx(36, abs=1)
    assert two.y0 - one.y0 == pytest.approx(8 * 72 / 25.4, abs=1)


@pytest.mark.parametrize("xdp", [b"<xdp:xdp/>", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xdp>&a;</xdp>'])
def test_unusable_xfa_falls_back_to_the_placeholder(tmp_path, xdp):
    writer = PdfWriter()
    writer.add_blank_page(300, 300)
    stream = DecodedStreamObject()
    stream.set_data(xdp)
    writer.root_object[NameObject("/AcroForm")] = DictionaryObject({
        NameObject("/Fields"): ArrayObject(), NameObject("/XFA"): writer._add_object(stream)})
    path = tmp_path / "broken.pdf"
    writer.write(path)
    doc = Document(path)
    assert doc.xfa and not doc.xfa_layout and "could not lay out" in doc.notice
    assert len(doc.pages) == 1 and doc.suggested_path == doc.path
    with pytest.raises(ValueError):
        convert(path.read_bytes())


def test_measurements_and_pictures():
    assert measure("1in") == 72 and measure("25.4mm") == pytest.approx(72) and measure("2") == 144
    assert measure("10pt") == 10 and measure("bogus", None) is None
    assert format_number("zzz,zz9.99", "1234.5") == "1,234.50"
    assert format_number("z,zzz,zz9", "-42") == "-42"
    assert format_number("9999", "7") == "0007"
    assert format_date("DD MMM YYYY", "2026-02-28") == "28 Feb 2026"
    assert format_date("YYYY/MM/DD", "20260228") == "2026/02/28"
    assert format_text("999-999", "123456") == "123-456"
