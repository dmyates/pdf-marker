"""How field values are shown: choice-list items and XFA display pictures."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .model import Node, items_of, ui_of

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def display(node: Node) -> str:
    """The text a field shows for its current value."""
    value = node.value or ""
    ui = ui_of(node.el)
    kind = ui.tag if ui is not None else "textEdit"
    if kind == "choiceList" and node.items:
        value = next((label for saved, label in node.items if saved == value), value)
    elif kind == "choiceList":
        lists = node.el.findall("items")
        shown = next((items for items in lists if items.get("save") != "1"), None)
        saved = next((items for items in lists if items.get("save") == "1"), None)
        if shown is not None and saved is not None:
            values = [c.text or "" for c in saved]
            if value in values and values.index(value) < len(shown):
                value = shown[values.index(value)].text or ""
    if kind == "passwordEdit":
        return "*" * len(value)
    fmt = node.el.find("format")
    picture = fmt.find("picture") if fmt is not None else None
    if value and picture is not None and (picture.text or "").strip():
        formatted = format_picture(picture.text.strip(), value)
        if formatted is not None:
            return formatted
    return value


def format_picture(picture: str, value: str) -> str | None:
    """Apply the first alternative of an XFA picture clause, or None if it does not apply."""
    for alternative in picture.split("|"):
        alternative = alternative.strip()
        match = re.fullmatch(r"(num|date|time|text|datetime)(?:\.[a-z]+)?\{(.*)\}", alternative, re.S)
        kind, body = (match[1], match[2]) if match else (None, alternative)
        if match and not body and "." in alternative.split("{")[0]:
            return None  # Locale-defined pictures: the raw value is the best we can do.
        try:
            if kind == "num" or (kind is None and re.fullmatch(r"[9zZsS,.$()v\s-]+", body)):
                return format_number(body, value)
            if kind == "date":
                return format_date(body, value)
            if kind == "text" or kind is None:
                return format_text(body, value)
        except (ValueError, InvalidOperation, IndexError):
            continue
    return None


def format_number(picture: str, value: str) -> str:
    number = Decimal(value.strip().replace(" ", ""))
    radix = re.search(r"[.vV]", picture)
    int_part, frac_part = (picture[:radix.start()], picture[radix.end():]) if radix else (picture, "")
    places = len(re.findall(r"[9zZ]", frac_part))
    number = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    negative = number < 0
    digits_int, _, digits_frac = f"{abs(number):f}".partition(".")
    digits_int = digits_int.lstrip("0")
    out = []
    pending = list(digits_int)
    for symbol in reversed(int_part):
        if symbol in "9zZ":
            if pending:
                out.append(pending.pop())
            else:
                out.append("0" if symbol == "9" else ("" if symbol == "z" else " "))
        elif symbol == ",":
            out.append("," if pending else "")
        elif symbol in "sS":
            out.append("-" if negative else ("" if symbol == "s" else " "))
            negative = False
        elif symbol == "(":
            out.append("(" if number < 0 else " ")
        elif symbol == ")":
            out.append(")" if number < 0 else " ")
        else:
            out.append(symbol)
    if pending:
        out.append("".join(pending)[::-1])
    result = "".join(reversed(out))
    if negative:
        result = "-" + result.lstrip()
    if radix:
        frac = list(digits_frac.ljust(places, "0"))
        tail = []
        for symbol in frac_part:
            if symbol in "9zZ":
                tail.append(frac.pop(0) if frac else "0")
            elif symbol in "sS":
                tail.append("-" if number < 0 else "")
            else:
                tail.append(symbol)
        result += ("." if radix.group() in ".vV" else radix.group()) + "".join(tail)
    return result.strip() if result.strip() else "0"


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y%m%d", 8), ("%Y/%m/%d", 10), ("%d/%m/%Y", 10)):
        try:
            return datetime.strptime(value[:size], fmt).date()
        except ValueError:
            continue
    raise ValueError(value)


def format_date(picture: str, value: str) -> str:
    day = _parse_date(value)
    tokens = re.findall(r"'[^']*'|YYYY|YY|MMMM|MMM|MM|M|DD|D|EEEE|EEE|.", picture)
    out = []
    for token in tokens:
        out.append({
            "YYYY": f"{day.year:04d}", "YY": f"{day.year % 100:02d}", "MMMM": MONTHS[day.month - 1],
            "MMM": MONTHS[day.month - 1][:3], "MM": f"{day.month:02d}", "M": str(day.month),
            "DD": f"{day.day:02d}", "D": str(day.day), "EEEE": DAYS[day.weekday()], "EEE": DAYS[day.weekday()][:3],
        }.get(token, token[1:-1] if token.startswith("'") and len(token) > 1 else token))
    return "".join(out)


def format_text(picture: str, value: str) -> str:
    chars = list(value)
    out = []
    for token in re.findall(r"'[^']*'|.", picture):
        if token in ("9", "A", "X", "O", "0"):
            out.append(chars.pop(0) if chars else "")
        else:
            out.append(token[1:-1] if token.startswith("'") and len(token) > 1 else token)
    if chars:
        raise ValueError(value)
    return "".join(out)


def check_state(node: Node) -> bool:
    items = items_of(node.el)
    on = items[0][0] if items and items[0] else "1"
    return node.value not in (None, "") and node.value == on
