"""Fonts and text layout for XFA content: plain values and XHTML rich text."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import groupby

import pymupdf as pdf

from .model import Font, Para, measure

# Built-in PDF fonts need no embedding. Their ascent and descent are those of
# the metric-compatible Windows fonts, so line heights match Adobe's layout.
BASE14 = {
    "sans": ("helv", "hebo", "heit", "hebi"),
    "serif": ("tiro", "tibo", "tiit", "tibi"),
    "mono": ("cour", "cobo", "coit", "cobi"),
}
METRICS = {"sans": (0.905, 0.212), "serif": (0.891, 0.216), "mono": (0.833, 0.300)}
FAMILIES = {
    "arial": "sans", "helvetica": "sans", "arial mt": "sans", "liberation sans": "sans", "arimo": "sans",
    "times": "serif", "times new roman": "serif", "times-roman": "serif", "liberation serif": "serif",
    "courier": "mono", "courier new": "mono", "courier std": "mono", "liberation mono": "mono",
}
# Other common form fonts, with installed alternatives to look for first.
ALTERNATIVES = {
    "arial narrow": ["Liberation Sans Narrow", "Nimbus Sans Narrow", "Arial Narrow"],
    "arial black": ["Arial Black"],
    "myriad pro": ["Myriad Pro"],
    "minion pro": ["Minion Pro"],
    "verdana": ["Verdana", "DejaVu Sans"],
    "tahoma": ["Tahoma", "DejaVu Sans"],
    "calibri": ["Calibri", "Carlito"],
    "cambria": ["Cambria", "Caladea"],
    "georgia": ["Georgia", "Gelasio"],
}
# Fonts without an installed alternative fall back to a built-in font, with
# heavy faces drawn bold.
HEAVY = ("black", "heavy", "extra bold", "ultra")
FALLBACK = "\x00fallback"  # Any installed font that has the characters a typeface lacks.
FIT = 1.0  # Points of overflow tolerated before a line wraps.
BLOCKS = ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6")


@dataclass(frozen=True)
class Face:
    font: pdf.Font
    ascent: float
    descent: float

    def width(self, text: str, size: float) -> float:
        return self.font.text_length(text, fontsize=size)


_lookups = [0]


@lru_cache(maxsize=None)
def _fc_match(pattern: str) -> tuple[str, str] | None:
    # Typefaces come from the document: keep them to plain names, and bound
    # how many a single process will look up.
    pattern = re.sub(r"[^\w .&'+:]", " ", pattern)[:120]
    if not shutil.which("fc-match") or _lookups[0] >= 64:
        return None
    _lookups[0] += 1
    try:
        out = subprocess.run(["fc-match", "--format=%{file}\n%{family}", pattern],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    path, _, families = out.partition("\n")
    return (path, families) if path else None


@lru_cache(maxsize=None)
def _file_face(path: str) -> Face | None:
    try:
        font = pdf.Font(fontfile=path)
    except Exception:
        return None
    return Face(font, font.ascender, -font.descender)


@lru_cache(maxsize=None)
def _base_face(kind: str, bold: bool, italic: bool) -> Face:
    name = BASE14[kind][bold + 2 * italic]
    ascent, descent = METRICS[kind]
    return Face(pdf.Font(name), ascent, descent)


@lru_cache(maxsize=None)
def face(typeface: str, bold: bool, italic: bool) -> Face:
    name = re.sub(r"\s+", " ", re.sub(r"[:,]", " ", typeface).strip().strip("'\"")).lower()
    kind = FAMILIES.get(name)
    if kind:
        return _base_face(kind, bold, italic)
    style = (":bold" if bold else "") + (":italic" if italic else "")
    if name == FALLBACK:
        found = _fc_match("sans-serif" + style)
        return (_file_face(found[0]) if found else None) or _base_face("sans", bold, italic)
    for family in ALTERNATIVES.get(name, [name]):
        found = _fc_match(family + style)
        if found and family.lower() in [f.strip().lower() for f in found[1].split(",")]:
            result = _file_face(found[0])
            if result:
                return result
    if "mono" in name or "courier" in name:
        kind = "mono"
    elif "times" in name or "serif" in name.replace("sans serif", "") or "roman" in name:
        kind = "serif"
    else:
        kind = "sans"
    return _base_face(kind, bold or any(word in name for word in HEAVY), italic)


def face_of(style: Font) -> Face:
    return face(style.typeface, style.bold, style.italic)


# Paragraphs and lines --------------------------------------------------------

@dataclass
class Paragraph:
    para: Para
    runs: list[tuple[str, Font]] = field(default_factory=list)
    style: Font | None = None  # Sets the height of an empty paragraph.


@dataclass
class Piece:
    x: float
    text: str
    style: Font
    width: float


@dataclass
class Line:
    pieces: list[Piece]
    width: float
    ascent: float
    descent: float
    height: float
    para: Para
    first: bool
    last: bool


@dataclass
class Block:
    lines: list[Line]
    width: float
    height: float


def plain(text: str, style: Font, para: Para) -> list[Paragraph]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [Paragraph(para, [(line, style)], style) for line in text.split("\n")]


def covered(runs: list[tuple[str, Font]]) -> list[tuple[str, Font]]:
    """Move characters a font cannot draw into runs of the fallback font."""
    result = []
    for text, style in runs:
        font = face_of(style).font
        if all(c.isspace() or font.has_glyph(ord(c)) for c in text):
            result.append((text, style))
            continue
        fallback = replace(style, typeface=FALLBACK)
        for missing, part in ((m, "".join(g)) for m, g in groupby(
                text, key=lambda c: not c.isspace() and not font.has_glyph(ord(c)))):
            result.append((part, fallback if missing else style))
    return result


def width_of(text: str, style: Font) -> float:
    width = face_of(style).width(text, style.size)
    return width + style.letter_spacing * len(text)


def layout(paragraphs: list[Paragraph], width: float | None) -> Block:
    """Break paragraphs into lines. With no width, lines only break at newlines."""
    lines: list[Line] = []
    for paragraph in paragraphs:
        para = paragraph.para
        tokens = [(tok, style) for text, style in covered(paragraph.runs)
                  for tok in re.findall(r"\n|[^\S\n]+|[^\s]+", text)]
        start = len(lines)
        current: list[Piece] = []
        x = 0.0

        def avail():
            if width is None:
                return None
            return width - para.left - para.right - (para.indent if len(lines) == start else 0)

        def flush():
            nonlocal current, x
            while current and not current[-1].text.strip() and len(current) > 1:
                current.pop()
            lines.append(_line(current, para, len(lines) == start, paragraph.style))
            current, x = [], 0.0

        for tok, style in tokens:
            if tok == "\n":
                flush()
                continue
            w = width_of(tok, style)
            limit = avail()
            if tok.isspace():
                if current or len(lines) == start:
                    current.append(Piece(x, tok, style, w))
                    x += w
                continue
            # Designer often sizes boxes to their text exactly, so allow for rounding.
            if limit is not None and x + w > limit + FIT and any(p.text.strip() for p in current):
                flush()
                limit = avail()
            while limit is not None and w > limit + FIT and len(tok) > 1 and not current:
                # A word wider than the line is broken between characters.
                cut = max(1, next((i for i in range(len(tok), 0, -1) if width_of(tok[:i], style) <= limit), 1))
                current.append(Piece(x, tok[:cut], style, width_of(tok[:cut], style)))
                flush()
                tok = tok[cut:]
                w = width_of(tok, style)
                limit = avail()
            current.append(Piece(x, tok, style, w))
            x += w
        flush()
        lines[-1].last = True
    for line in lines:
        line.pieces = _merge(line.pieces)
    height = sum(line.height for line in lines) + sum(
        (p.para.above + p.para.below) for p in paragraphs)
    width_used = max((line.width + line.para.left + line.para.right + (line.para.indent if line.first else 0)
                      for line in lines), default=0.0)
    return Block(lines, width_used, height)


def _line(pieces: list[Piece], para: Para, first: bool, style: Font | None) -> Line:
    if pieces and not pieces[0].text.strip() and len(pieces) > 1 and not first:
        offset = pieces[0].width
        pieces = [Piece(p.x - offset, p.text, p.style, p.width) for p in pieces[1:]]
    styles = [p.style for p in pieces] or ([style] if style else [])
    ascent = max((face_of(s).ascent * s.size for s in styles), default=0.0)
    descent = max((face_of(s).descent * s.size for s in styles), default=0.0)
    width = (pieces[-1].x + pieces[-1].width) if pieces else 0.0
    if pieces and not pieces[-1].text.strip():
        width = pieces[-1].x
    height = para.line_height or (ascent + descent)
    return Line(pieces, width, ascent, descent, height, para, first, False)


def _merge(pieces: list[Piece]) -> list[Piece]:
    merged: list[Piece] = []
    for piece in pieces:
        if merged and merged[-1].style == piece.style and abs(merged[-1].x + merged[-1].width - piece.x) < 0.01:
            last = merged[-1]
            merged[-1] = Piece(last.x, last.text + piece.text, last.style, last.width + piece.width)
        else:
            merged.append(piece)
    return merged


def empty_line_height(style: Font, para: Para) -> float:
    f = face_of(style)
    return para.line_height or (f.ascent + f.descent) * style.size


# Rich text ---------------------------------------------------------------

def _css(style: str) -> dict[str, str]:
    result = {}
    for item in style.split(";"):
        key, _, value = item.partition(":")
        if key.strip():
            result[key.strip().lower()] = value.strip()
    return result


def _css_color(value: str, default):
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
    if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        return tuple(int(c * 2, 16) / 255 for c in value[1:])
    parts = re.findall(r"\d+", value)
    if value.startswith("rgb") and len(parts) >= 3:
        return tuple(min(255, int(p)) / 255 for p in parts[:3])
    return {"black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "blue": (0, 0, 1)}.get(value.lower(), default)


def _size(value: str, default: float) -> float:
    value = value.strip()
    if value.endswith("%"):
        try:
            return default * float(value[:-1]) / 100
        except ValueError:
            return default
    if value.endswith("em"):
        try:
            return default * float(value[:-2])
        except ValueError:
            return default
    return measure(value if re.search(r"[a-z]$", value) else value + "pt", default)


def _apply(css: dict[str, str], style: Font, para: Para) -> tuple[Font, Para, bool]:
    spacerun = css.get("xfa-spacerun") == "yes"
    changes = {}
    if "font-family" in css:
        changes["typeface"] = css["font-family"].split(",")[0].strip().strip("'\"") or style.typeface
    if "font-size" in css:
        changes["size"] = _size(css["font-size"], style.size)
    if "font-weight" in css:
        weight = css["font-weight"].lower()
        changes["bold"] = weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600)
    if "font-style" in css:
        changes["italic"] = css["font-style"].lower() in ("italic", "oblique")
    if "color" in css:
        changes["color"] = _css_color(css["color"], style.color)
    if "text-decoration" in css:
        decoration = css["text-decoration"].lower()
        changes["underline"] = 1 if "underline" in decoration else 0
        changes["strike"] = 1 if "line-through" in decoration else 0
    if "letter-spacing" in css:
        changes["letter_spacing"] = _size(css["letter-spacing"], 0.0) if css["letter-spacing"] != "normal" else 0.0
    if "vertical-align" in css:
        align = css["vertical-align"].lower()
        if align in ("super", "sub"):
            changes["baseline_shift"] = style.size * (0.33 if align == "super" else -0.15)
            changes["size"] = changes.get("size", style.size) * 0.66
    pchanges = {}
    for key, attr in (("margin-top", "above"), ("margin-bottom", "below"), ("margin-left", "left"),
                      ("margin-right", "right"), ("text-indent", "indent"), ("line-height", "line_height")):
        if key in css:
            pchanges[attr] = _size(css[key], getattr(para, attr) if attr != "line_height" else style.size)
    if re.fullmatch(r"\d+(\.\d+)?", css.get("line-height", "")):
        pchanges["line_height"] = float(css["line-height"]) * changes.get("size", style.size)
    if "text-align" in css:
        pchanges["h"] = css["text-align"].lower()
    return replace(style, **changes) if changes else style, replace(para, **pchanges) if pchanges else para, spacerun


def rich(body, style: Font, para: Para) -> list[Paragraph]:
    """XHTML rich text (an <exData> body) as paragraphs."""
    paragraphs: list[Paragraph] = []
    current: list[Paragraph | None] = [None]

    def ensure(p: Para, s: Font) -> Paragraph:
        if current[0] is None:
            current[0] = Paragraph(p, style=s)
            paragraphs.append(current[0])
        return current[0]

    def text(value: str | None, s: Font, p: Para, spacerun: bool):
        if not value:
            return
        if not spacerun:
            value = re.sub(r"\s+", " ", value)
            target = current[0]
            if value.startswith(" ") and (target is None or not target.runs or target.runs[-1][0].endswith((" ", "\n"))):
                value = value[1:]
        if value:
            ensure(p, s).runs.append((value, s))

    def walk(el, s: Font, p: Para, spacerun: bool):
        tag = el.tag
        css = _css(el.get("style", ""))
        if tag in ("b", "strong"):
            s = replace(s, bold=True)
        elif tag in ("i", "em"):
            s = replace(s, italic=True)
        elif tag == "u":
            s = replace(s, underline=1)
        elif tag in ("sup", "sub"):
            css.setdefault("vertical-align", "super" if tag == "sup" else "sub")
        s, p, own_spacerun = _apply(css, s, p)
        spacerun = spacerun or own_spacerun
        block = tag in BLOCKS
        if block:
            current[0] = None
            if tag == "li":
                text("• ", s, p, True)
        before = len(paragraphs)
        if tag == "br":
            ensure(p, s).runs.append(("\n", s))
        else:
            text(el.text, s, p, spacerun)
            for child in el:
                walk(child, s, p, spacerun)
                text(child.tail, s, p, spacerun)
        if block:
            # An empty paragraph is still a blank line, unless it only held other blocks.
            if len(paragraphs) == before and not any(c.tag in BLOCKS + ("ul", "ol") for c in el):
                ensure(p, s)
            current[0] = None

    root = body.find("body") if body.tag != "body" else body
    if root is None:
        root = body
    walk(root, style, para, False)
    for paragraph in paragraphs:
        # Trailing collapsible spaces do not count.
        while paragraph.runs and paragraph.runs[-1][0].strip(" ") == "" and paragraph.runs[-1][0] != "\n":
            paragraph.runs.pop()
    return paragraphs or [Paragraph(para, [], style)]
