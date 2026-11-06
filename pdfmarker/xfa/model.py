"""XFA packets, template properties, and the merge of template with data.

The template describes the form's containers (subforms, fields, draws) and the
datasets packet holds the values. Merging them produces a tree of `Node`s, one
per container instance, which the layout engine then places on pages.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass

UNITS = {"in": 72.0, "pt": 1.0, "mm": 72 / 25.4, "cm": 72 / 2.54, "mp": 0.001, "px": 1.0}
VALUE_TAGS = ("text", "integer", "decimal", "float", "date", "dateTime", "time", "boolean")
MAX_INSTANCES = 2000


def local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def parse_xml(data: bytes) -> ET.Element:
    """Parse XML without namespaces. XFA never needs a DTD, so one is refused."""
    if re.search(rb"<!(DOCTYPE|ENTITY)", data, re.I):
        raise ValueError("XFA data must not declare a DTD.")
    # Adobe Reader accepts a bare "&" in data, so it does turn up in real forms.
    data = re.sub(rb"&(?!(?:[A-Za-z_][\w.-]*|#[0-9]+|#x[0-9A-Fa-f]+);)", b"&amp;", data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(f"The XFA data is not valid XML: {error}") from None
    for el in root.iter():
        el.tag = local(el.tag)
        if any("}" in key for key in el.attrib):
            el.attrib = {local(k): v for k, v in el.attrib.items()}
    return root


def measure(value: str | None, default: float | None = 0.0) -> float | None:
    """An XFA measurement in points. Unitless values are inches.

    A number ending in a bare point, like "1162.mm" in a real form, is invalid
    and gets the default, as it evidently does in Adobe Reader."""
    if value is None:
        return default
    match = re.fullmatch(r"\s*([-+]?(?:\d+(?:\.\d+)?|\.\d+))\s*([a-z]*)\s*", value)
    if not match:
        return default
    return float(match[1]) * UNITS.get(match[2] or "in", 72.0)


def color(el: ET.Element | None, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    """The colour of an element holding a <color value="r,g,b"> child."""
    node = el.find("color") if el is not None else None
    if node is None:
        return default
    parts = re.findall(r"\d+", node.get("value", ""))
    if len(parts) < 3:
        return (0.0, 0.0, 0.0) if node.get("value") is None else default
    return tuple(min(255, int(p)) / 255 for p in parts[:3])


def integer(value: str | None, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Template properties ------------------------------------------------------

@dataclass(frozen=True)
class Font:
    typeface: str = "Courier"
    size: float = 10.0
    bold: bool = False
    italic: bool = False
    color: tuple = (0.0, 0.0, 0.0)
    underline: int = 0
    strike: int = 0
    letter_spacing: float = 0.0
    baseline_shift: float = 0.0


def font_of(el: ET.Element | None, base: Font | None = None) -> Font:
    base = base or Font()
    node = el.find("font") if el is not None else None
    if node is None:
        return base
    fill = node.find("fill")
    return Font(
        node.get("typeface", base.typeface).strip() or base.typeface,
        measure(node.get("size"), base.size) or base.size,
        node.get("weight", "bold" if base.bold else "normal") == "bold",
        node.get("posture", "italic" if base.italic else "normal") == "italic",
        color(fill, base.color) if fill is not None else base.color,
        integer(node.get("underline"), base.underline),
        integer(node.get("lineThrough"), base.strike),
        measure(node.get("letterSpacing"), base.letter_spacing) or 0.0,
        measure(node.get("baselineShift"), base.baseline_shift) or 0.0,
    )


@dataclass(frozen=True)
class Para:
    h: str = "left"
    v: str = "top"
    left: float = 0.0
    right: float = 0.0
    above: float = 0.0
    below: float = 0.0
    indent: float = 0.0
    line_height: float = 0.0


def para_of(el: ET.Element | None, base: Para | None = None) -> Para:
    base = base or Para()
    node = el.find("para") if el is not None else None
    if node is None:
        return base
    return Para(
        node.get("hAlign", base.h), node.get("vAlign", base.v),
        measure(node.get("marginLeft"), base.left), measure(node.get("marginRight"), base.right),
        measure(node.get("spaceAbove"), base.above), measure(node.get("spaceBelow"), base.below),
        measure(node.get("textIndent"), base.indent), measure(node.get("lineHeight"), base.line_height),
    )


def margin_of(el: ET.Element | None) -> tuple[float, float, float, float]:
    """Insets as (top, right, bottom, left)."""
    node = el.find("margin") if el is not None else None
    if node is None:
        return (0.0, 0.0, 0.0, 0.0)
    return tuple(measure(node.get(k)) for k in ("topInset", "rightInset", "bottomInset", "leftInset"))


@dataclass(frozen=True)
class Stroke:
    visible: bool = True
    thickness: float = 0.5
    color: tuple = (0.0, 0.0, 0.0)
    style: str = "solid"


@dataclass(frozen=True)
class Outline:
    """A border, rectangle or arc: edges top, right, bottom, left, plus fill."""
    edges: tuple[Stroke, Stroke, Stroke, Stroke]
    radius: float = 0.0
    fill: tuple | None = None
    hand: str = "even"

    @property
    def visible(self) -> bool:
        return self.fill is not None or any(e.visible and e.thickness > 0 for e in self.edges)


def stroke_of(node: ET.Element | None) -> Stroke:
    if node is None:
        return Stroke()
    return Stroke(node.get("presence", "visible") == "visible", measure(node.get("thickness"), 0.5),
                  color(node), node.get("stroke", "solid"))


def fill_of(node: ET.Element | None, white: bool):
    """A fill colour. A fill without a colour is white for widgets and shapes but
    transparent for the borders of containers, as in Adobe Reader."""
    if node is None or node.get("presence", "visible") != "visible":
        return None
    if node.find("color") is None and not white and not any(node.find(k) is not None for k in ("linear", "radial", "pattern", "stipple")):
        return None
    base = color(node, (1.0, 1.0, 1.0))
    for kind in ("linear", "radial", "pattern", "stipple"):
        other = node.find(kind)
        if other is not None:
            # Gradients and patterns are drawn as the average of their two colours.
            end = color(other, (0.0, 0.0, 0.0))
            return tuple((a + b) / 2 for a, b in zip(base, end))
    return base


def outline_of(node: ET.Element | None, white: bool = False) -> Outline | None:
    """Read a <border>, <rectangle> or <arc>. Missing edges repeat the last one.
    `white` gives a colourless fill its widget and shape default."""
    if node is None or node.get("presence", "visible") != "visible":
        return None
    edges = [stroke_of(e) for e in node.findall("edge")] or [Stroke()]
    edges += [edges[-1]] * (4 - len(edges))
    corner = node.find("corner")
    radius = 0.0
    if corner is not None and corner.get("presence", "visible") == "visible":
        radius = measure(corner.get("radius")) or 0.0
    return Outline(tuple(edges[:4]), radius, fill_of(node.find("fill"), white), node.get("hand", "even"))


@dataclass(frozen=True)
class Caption:
    placement: str
    reserve: float | None
    margin: tuple
    font: Font
    para: Para
    text: str
    rich: ET.Element | None


def caption_of(el: ET.Element, font: Font, para: Para) -> Caption | None:
    node = el.find("caption")
    if node is None or node.get("presence", "visible") in ("hidden", "inactive"):
        return None
    text, rich = template_value(node)
    if not text and rich is None:
        return None
    reserve = measure(node.get("reserve"), None)
    return Caption(node.get("placement", "left"), reserve if reserve and reserve > 0 else None,
                   margin_of(node), font_of(node, font), para_of(node, para), text or "", rich)


def template_value(el: ET.Element) -> tuple[str | None, ET.Element | None]:
    """The default value of a field, draw or caption: plain text, or rich content."""
    value = el.find("value")
    if value is None:
        return None, None
    for child in value:
        if child.tag in VALUE_TAGS:
            return child.text or "", None
        if child.tag in ("exData", "image", "line", "rectangle", "arc"):
            text = "".join(child.itertext()) if child.tag == "exData" else None
            return text, child
    return None, None


def ui_of(el: ET.Element) -> ET.Element | None:
    ui = el.find("ui")
    if ui is None:
        return None
    return next((c for c in ui if c.tag not in ("extras", "picture")), None)


# The merged form ------------------------------------------------------------

class Node:
    """One instance of a template container, bound to data where applicable."""

    def __init__(self, el: ET.Element, parent: Node | None, data: ET.Element | None = None):
        self.el = el
        self.tag = el.tag
        self.parent = parent
        self.children: list[Node] = []
        self.name = el.get("name", "")
        self.data = data
        self.value: str | None = None
        self.content: ET.Element | None = None  # exData or image value
        self.items: list[tuple[str, str]] | None = None  # (value, label) from <bindItems>
        self.presence = el.get("presence", "visible")
        self.index = 0

    def find(self, name: str):
        return self.el.find(name)

    @property
    def visible(self) -> bool:
        return self.presence not in ("hidden", "inactive")

    @property
    def path(self) -> str:
        parts, node = [], self
        while node is not None:
            if node.tag != "pageSet":
                parts.append(f"{node.name or '#' + node.tag}[{node.index}]")
            node = node.parent
        return ".".join(reversed(parts))

    def __repr__(self):
        return f"<{self.tag} {self.path}>"


def is_group(el: ET.Element) -> bool:
    kind = el.get("dataNode")
    return kind == "dataGroup" or (kind != "dataValue" and len(el) > 0)


def data_text(el: ET.Element) -> str:
    return "".join(el.itertext()) if len(el) else (el.text or "")


def som_steps(ref: str) -> list[str]:
    return re.findall(r"(?:[^.\[\]]|\[[^\]]*\])+", ref)


def resolve(ref: str, scope, record, data, datasets=None) -> list[ET.Element]:
    """Resolve a data reference such as `$.A.B[*].C` against the data DOM."""
    steps = som_steps(ref.strip())
    if not steps:
        return []
    head = steps[0]
    if head == "$":
        nodes, steps = [scope], steps[1:]
    elif head == "$record":
        nodes, steps = [record], steps[1:]
    elif head == "$data":
        nodes, steps = [data], steps[1:]
    elif steps[:3] in (["xfa", "datasets", "data"], ["$xfa", "datasets", "data"]):
        nodes, steps = [data], steps[3:]
    elif steps[:2] in (["xfa", "datasets"], ["$xfa", "datasets"]):
        nodes, steps = [datasets], steps[2:]
    elif head.startswith("!"):  # Shorthand for xfa.datasets.
        nodes, steps = [datasets], ([head[1:]] if head[1:] else []) + steps[1:]
    elif head.startswith("$"):
        return []
    else:
        nodes = [scope]
    nodes = [n for n in nodes if n is not None]
    for step in steps:
        match = re.fullmatch(r"([^\[]+)(?:\[\s*(\*|[-+]?\d+)\s*\])?", step)
        if not match:
            return []
        name, index = match[1].strip(), match[2]
        found = []
        for node in nodes:
            kids = [c for c in node if c.tag == name]
            if index == "*":
                found.extend(kids)
            else:
                i = integer(index, 0)
                if 0 <= i < len(kids):
                    found.append(kids[i])
        nodes = found
    return nodes


def binding(el: ET.Element) -> tuple[str, str]:
    bind = el.find("bind")
    if bind is None:
        return "once", ""
    return bind.get("match", "once"), bind.get("ref", "")


def occurrences(el: ET.Element) -> tuple[int, int, int]:
    occur = el.find("occur")
    if occur is None:
        return 1, 1, 1
    low = max(0, integer(occur.get("min"), 1))
    high = integer(occur.get("max"), max(low, 1))
    high = -1 if high < 0 else max(high, low)
    initial = max(low, integer(occur.get("initial"), low))
    if high >= 0:
        initial = min(initial, high)
    return low, high, initial


def resolve_protos(template: ET.Element):
    """Apply `use`/`usehref` references to prototypes within the template."""
    ids = {el.get("id"): el for el in template.iter() if el.get("id")}
    done = set()

    def apply(el, depth=0):
        if id(el) in done or depth > 20:
            return
        done.add(id(el))
        ref = el.get("use") or el.get("usehref") or ""
        match = re.fullmatch(r"\.?#(?:som\(\$template\.#?)?([^)]+)\)?", ref.strip())
        proto = ids.get(match[1]) if match else None
        if proto is None or proto is el:
            return
        apply(proto, depth + 1)
        merge_into(el, proto)

    def merge_into(el, proto):
        for key, value in proto.attrib.items():
            if key not in ("id", "name", "use", "usehref") and key not in el.attrib:
                el.set(key, value)
        mine = {c.tag: c for c in el}
        for child in proto:
            if child.tag in ("subform", "field", "draw", "exclGroup", "subformSet", "area"):
                el.append(deepcopy(child))
            elif child.tag in mine:
                merge_into(mine[child.tag], child)
            else:
                el.append(deepcopy(child))

    for el in list(template.iter()):
        if el.get("use") or el.get("usehref"):
            apply(el)


class Merge:
    """Build the form tree: template containers instantiated against data."""

    def __init__(self, datasets: ET.Element | None):
        data = datasets.find("data") if datasets is not None else None
        self.datasets = datasets
        self.data = data
        self.record = next(iter(data), None) if data is not None else None
        self.bound: set[int] = set()
        self.count = 0

    def form(self, template: ET.Element) -> Node:
        root_el = template.find("subform")
        if root_el is None:
            raise ValueError("The XFA template has no root subform.")
        root = Node(root_el, None, self.record)
        self.fill(root, root_el, self.record)
        return root

    def fill(self, node: Node, el: ET.Element, scope):
        for child in el:
            tag = child.tag
            if tag == "subformSet":
                self.fill(node, child, scope)
            elif tag == "subform":
                node.children.extend(self.instances(child, node, scope))
            elif tag in ("field", "exclGroup"):
                node.children.append(self.field(child, node, scope))
            elif tag == "draw":
                node.children.append(self.draw(child, node))
            elif tag in ("area", "pageSet", "pageArea"):
                inner = Node(child, node, scope)
                node.children.append(inner)
                self.fill(inner, child, scope)
            elif tag == "contentArea":
                node.children.append(Node(child, node))
            self.count += 1
            if self.count > 200_000:
                raise ValueError("This XFA form is too large.")

    def draw(self, el: ET.Element, parent: Node) -> Node:
        node = Node(el, parent)
        node.value, node.content = template_value(el)
        return node

    def instances(self, el: ET.Element, parent: Node, scope) -> list[Node]:
        low, high, initial = occurrences(el)
        match, ref = binding(el)
        name = el.get("name")
        matches = None
        if self.data is not None:
            if match == "dataRef" and ref:
                found = resolve(ref, scope, self.record, self.data, self.datasets)
                matches = found if "[*]" in (som_steps(ref) or [""])[-1] else found[:1]
            elif match in ("once", "global") and name:
                found = [c for c in scope if c.tag == name and is_group(c) and id(c) not in self.bound] if scope is not None else []
                matches = found if high != 1 else found[:1]
        if matches is None:
            count, datas, inner = initial, [None] * initial, [scope] * initial
        else:
            if high >= 0:
                matches = matches[:high]
            self.bound.update(id(m) for m in matches)
            count = max(len(matches), low)
            datas = list(matches) + [None] * (count - len(matches))
            inner = datas
        nodes = []
        for i in range(min(count, MAX_INSTANCES)):
            node = Node(el, parent, datas[i])
            node.index = i
            self.fill(node, el, inner[i])
            nodes.append(node)
        return nodes

    def bound_items(self, el: ET.Element, scope) -> list[tuple[str, str]] | None:
        """List items taken from data by <bindItems>: declarative, so no script runs."""
        spec = el.find("bindItems")
        if spec is None or not spec.get("ref") or self.datasets is None:
            return None
        nodes = resolve(spec.get("ref"), scope, self.record, self.data, self.datasets)[:MAX_INSTANCES * 5]
        label, value = spec.get("labelRef", "$"), spec.get("valueRef", spec.get("labelRef", "$"))
        return [(item_part(n, value), item_part(n, label)) for n in nodes]

    def field(self, el: ET.Element, parent: Node, scope) -> Node:
        node = Node(el, parent)
        node.value, node.content = template_value(el)
        match, ref, name = *binding(el), el.get("name")
        found = None
        if self.data is not None:
            if match == "dataRef" and ref:
                candidates = resolve(ref, scope, self.record, self.data, self.datasets)
                found = next((c for c in candidates if id(c) not in self.bound), candidates[0] if candidates else None)
            elif match == "global" and name:
                # The nearest value of that name, or else the first anywhere in the record.
                nearby = (c for c in scope if c.tag == name) if scope is not None else iter(())
                anywhere = (c for c in self.record.iter(name) if not is_group(c)) if self.record is not None else iter(())
                found = next(nearby, None) or next(anywhere, None)
            elif match == "once" and name and scope is not None:
                found = next((c for c in scope if c.tag == name and not is_group(c) and id(c) not in self.bound), None)
        if found is not None:
            self.bound.add(id(found))
            node.data = found
            node.value, node.content = data_text(found), None
        node.items = self.bound_items(el, scope)
        if el.tag == "exclGroup":
            chosen = node.value
            for child in el:
                if child.tag != "field":
                    continue
                member = Node(child, node)
                member.value, member.content = template_value(child)
                on = on_value(child)
                if chosen is None and member.value not in (None, "") and member.value == on:
                    chosen = on
                node.children.append(member)
            node.value = chosen
            for member in node.children:
                member.value = on_value(member.el) if chosen not in (None, "") and chosen == on_value(member.el) else off_value(member.el)
        return node


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


def compensate(root: Node):
    """Stand in for the two commonest jobs of form scripts, which are never run.

    Forms often hide a section until a script finds data for it, and often keep
    a value in a hidden field that a script copies into a visible field drawn
    at the same place. Without these steps that data would not appear at all.
    """
    def filled(node: Node) -> bool:
        return node.data is not None and bool((node.value or "").strip())

    def holds_data(node: Node) -> bool:
        return any(n.tag == "field" and n.el.get("presence", "visible") == "visible" and filled(n)
                   for n in walk(node) if n is not node)

    for node in list(walk(root)):
        if node.tag == "subform" and node.presence == "hidden" and holds_data(node):
            node.presence = "visible"
    tolerance = UNITS["mm"]
    for node in walk(root):
        fields = [c for c in node.children if c.tag == "field"]
        sources = [f for f in fields if not f.visible and filled(f)]
        for target in fields:
            if not target.visible or target.data is not None or (target.value or "").strip():
                continue
            ui = ui_of(target.el)
            if ui is not None and ui.tag not in ("textEdit", "numericEdit", "dateTimeEdit"):
                continue
            tx, ty = measure(target.el.get("x")), measure(target.el.get("y"))
            th = measure(target.el.get("h"), 0.0) or 0.0
            matches = [s for s in sources if abs(measure(s.el.get("x")) - tx) <= tolerance
                       and measure(s.el.get("y")) < ty + max(th, tolerance)
                       and ty < measure(s.el.get("y")) + max(measure(s.el.get("h"), 0.0) or 0.0, tolerance)]
            if len(matches) == 1:
                target.value = matches[0].value


def item_part(node: ET.Element, ref: str) -> str:
    """One part of a bound list item: the node's value, an attribute or a child."""
    ref = ref.strip()
    if ref in ("", "$"):
        return data_text(node)
    name = ref[2:] if ref.startswith("$.") else ref
    if name in node.attrib:
        return node.get(name)
    child = node.find(name) if re.fullmatch(r"[\w.-]+", name) else None
    return data_text(child) if child is not None else ""


def items_of(el: ET.Element) -> list[list[str]]:
    """Every <items> list of a field: display items and, if present, saved values."""
    return [[c.text or "" for c in items] for items in el.findall("items")]


def on_value(el: ET.Element) -> str:
    items = items_of(el)
    return items[0][0] if items and items[0] else "1"


def off_value(el: ET.Element) -> str:
    items = items_of(el)
    return items[0][1] if items and len(items[0]) > 1 else ("0" if not items else "")


def access_of(node: Node) -> str:
    """The most restrictive access of a node and its containers."""
    order = {"open": 0, "protected": 1, "readOnly": 2, "nonInteractive": 3}
    result = "open"
    while node is not None:
        value = node.el.get("access", "open")
        if order.get(value, 0) > order[result]:
            result = value
        node = node.parent
    return result
