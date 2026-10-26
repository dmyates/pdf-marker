"""Portable clipboard payloads for editable marks, including vector text."""
import base64
from dataclasses import asdict
import json
from math import isfinite
from .document import Mark

MIME = 'application/x-pdfmarker-marks+json'
MAX_BYTES = 64 * 1024 * 1024


def encode_marks(marks):
    items = []
    for mark in marks:
        item = asdict(mark)
        item['appearance'] = base64.b64encode(mark.appearance).decode('ascii')
        items.append(item)
    return json.dumps({'version': 1, 'marks': items}, ensure_ascii=False).encode('utf-8')


def decode_marks(data):
    if len(data) > MAX_BYTES:
        raise ValueError('The clipboard selection is too large.')
    payload = json.loads(data)
    if payload['version'] != 1 or not payload['marks']:
        raise ValueError('The clipboard does not contain supported marks.')
    marks = []
    for item in payload['marks']:
        item = dict(item)
        item['rect'] = tuple(item['rect'])
        item['points'] = tuple(tuple(point) for point in item['points'])
        item['color'] = tuple(item['color'])
        item['appearance'] = base64.b64decode(item['appearance'], validate=True)
        mark = Mark(**item)
        if (mark.kind not in {'text', 'draw', 'tick', 'cross'}
                or type(mark.page) is not int or mark.page < 0
                or not isinstance(mark.text, str) or len(mark.rect) != 4
                or len(mark.color) != 3 or any(len(p) != 2 for p in mark.points)):
            raise ValueError('The clipboard contains an invalid mark.')
        numbers = (*mark.rect, *mark.color, mark.size, mark.width,
                   *(v for point in mark.points for v in point))
        if (not all(isinstance(v, (int, float)) and isfinite(v) for v in numbers)
                or mark.size <= 0 or mark.width <= 0
                or any(c < 0 or c > 1 for c in mark.color)
                or mark.rect[2] < mark.rect[0] or mark.rect[3] < mark.rect[1]):
            raise ValueError('The clipboard contains invalid mark geometry.')
        marks.append(mark)
    return marks
