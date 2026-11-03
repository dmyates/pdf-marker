"""Selectable page text in display coordinates, independent of the GUI.

A caret is an index between characters: caret i sits before character i.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Line:
    start: int
    end: int
    rect: tuple[float, float, float, float]
    direction: tuple[float, float]


@dataclass
class PageText:
    chars: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    @classmethod
    def from_rawdict(cls, raw: dict, matrix=None) -> PageText:
        """Build from PyMuPDF rawdict output; `matrix` maps unrotated to display points."""
        text = cls()

        def point(x, y):
            if matrix is None:
                return x, y
            return x*matrix.a + y*matrix.c + matrix.e, x*matrix.b + y*matrix.d + matrix.f

        def box(b):
            (x0, y0), (x1, y1) = point(b[0], b[1]), point(b[2], b[3])
            return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

        for block in raw.get('blocks', ()):
            if block.get('type', 0) != 0:
                continue
            for line in block.get('lines', ()):
                start = len(text.chars)
                for span in line.get('spans', ()):
                    for char in span.get('chars', ()):
                        c = char.get('c', '')
                        if c:
                            text.chars.append((*box(char['bbox']), c))
                if len(text.chars) == start:
                    continue
                dx, dy = line.get('dir', (1, 0))
                if matrix is not None:
                    dx, dy = dx*matrix.a + dy*matrix.c, dx*matrix.b + dy*matrix.d
                text.lines.append(Line(start, len(text.chars), box(line['bbox']), (dx, dy)))
        return text

    def __len__(self):
        return len(self.chars)

    def _line_of(self, index):
        for line in self.lines:
            if line.start <= index < line.end:
                return line
        return self.lines[-1] if self.lines else None

    def caret(self, x: float, y: float) -> int:
        """Nearest caret to a point, extending to the ends above or below all text."""
        if not self.lines:
            return 0
        if y < min(l.rect[1] for l in self.lines):
            return 0
        if y > max(l.rect[3] for l in self.lines):
            return len(self.chars)

        def distance(line):
            x0, y0, x1, y1 = line.rect
            dx = max(x0-x, 0, x-x1)
            dy = max(y0-y, 0, y-y1)
            return dy*4 + dx if line.direction[0] else dx*4 + dy

        line = min(self.lines, key=distance)
        dx, dy = line.direction
        position = x*dx + y*dy
        for i in range(line.start, line.end):
            x0, y0, x1, y1, _ = self.chars[i]
            if position < ((x0+x1)/2)*dx + ((y0+y1)/2)*dy:
                return i
        return line.end

    def hit(self, x: float, y: float, pad: float = 1) -> bool:
        return any(l.rect[0]-pad <= x <= l.rect[2]+pad and l.rect[1]-pad <= y <= l.rect[3]+pad for l in self.lines)

    def text(self, start: int = 0, end: int | None = None) -> str:
        end = len(self.chars) if end is None else end
        out = []
        for line in self.lines:
            a, b = max(start, line.start), min(end, line.end)
            if a < b:
                out.append(''.join(c[4] for c in self.chars[a:b]).rstrip() if b == line.end else ''.join(c[4] for c in self.chars[a:b]))
        return '\n'.join(out)

    def rects(self, start: int, end: int) -> list[tuple[float, float, float, float]]:
        """One rectangle per line segment, spanning the full line height."""
        result = []
        for line in self.lines:
            a, b = max(start, line.start), min(end, line.end)
            if a >= b:
                continue
            chars = self.chars[a:b]
            if line.direction[0]:
                result.append((min(c[0] for c in chars), line.rect[1], max(c[2] for c in chars), line.rect[3]))
            else:
                result.append((line.rect[0], min(c[1] for c in chars), line.rect[2], max(c[3] for c in chars)))
        return result

    def word(self, index: int) -> tuple[int, int]:
        line = self._line_of(index)
        if not line:
            return 0, 0
        index = max(line.start, min(index, line.end-1))
        start = end = index
        if self.chars[index][4].isspace():
            return index, index+1
        while start > line.start and not self.chars[start-1][4].isspace():
            start -= 1
        while end < line.end and not self.chars[end][4].isspace():
            end += 1
        return start, end

    def line_range(self, index: int) -> tuple[int, int]:
        line = self._line_of(index)
        return (line.start, line.end) if line else (0, 0)
