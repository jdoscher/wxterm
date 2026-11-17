"""Character canvas with a braille dot layer (2x4 dots per cell) and a text layer on top."""
from __future__ import annotations

from functools import lru_cache

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.strip import Strip

_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [dy][dx]
BAYER = ((0, 4), (6, 2), (1, 5), (7, 3))                             # 2x4 ordered dither thresholds 0..7


class Canvas:
    def __init__(self, w: int, h: int, style: str = ""):
        self.w, self.h = max(w, 0), max(h, 0)
        self.chars = [[" "] * self.w for _ in range(self.h)]
        self.cstyle = [[style] * self.w for _ in range(self.h)]
        self.text = [[False] * self.w for _ in range(self.h)]
        self.dots = [[0] * self.w for _ in range(self.h)]
        self.dstyle = [[style] * self.w for _ in range(self.h)]
        self.dprio = [[-1] * self.w for _ in range(self.h)]

    # --- dots (x in 0..2w, y in 0..4h) ---
    def dot(self, x: int, y: int, style: str, prio: int = 0):
        x, y = int(x), int(y)
        cx, cy = x >> 1, y >> 2
        if 0 <= cx < self.w and 0 <= cy < self.h and x >= 0 and y >= 0:
            self.dots[cy][cx] |= _BITS[y & 3][x & 1]
            if prio >= self.dprio[cy][cx]:
                self.dprio[cy][cx], self.dstyle[cy][cx] = prio, style

    def line(self, x0, y0, x1, y1, style: str, prio: int = 0, dash: int = 0):
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, n = dx + dy, 0
        while True:
            if not dash or (n // dash) % 2 == 0:
                self.dot(x0, y0, style, prio)
            n += 1
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def polyline(self, pts, style: str, prio: int = 0, dash: int = 0):
        for (a, b) in zip(pts, pts[1:]):
            self.line(a[0], a[1], b[0], b[1], style, prio, dash)

    # --- text ---
    def put(self, x: int, y: int, s: str, style: str = ""):
        if not 0 <= y < self.h:
            return
        for i, ch in enumerate(s):
            cx = x + i
            if 0 <= cx < self.w:
                self.chars[y][cx], self.cstyle[y][cx], self.text[y][cx] = ch, style, True

    def box(self, x, y, w, h, style: str, title: str = "", tstyle: str = "", right: str = ""):
        if w < 2 or h < 2:
            return
        self.put(x, y, "┌" + "─" * (w - 2) + "┐", style)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, "│", style)
            self.put(x + w - 1, yy, "│", style)
        self.put(x, y + h - 1, "└" + "─" * (w - 2) + "┘", style)
        if title:
            self.put(x + 2, y, f" {title} "[: w - 4], tstyle or style)
        if right and w > len(right) + len(title) + 10:
            self.put(x + w - len(right) - 4, y, f" {right} ", tstyle or style)

    def _runs(self, y: int):
        """(text, style) runs for one row: the text layer wins over dots."""
        run, rstyle = [], None
        chars, cstyle, text, dots, dstyle = self.chars[y], self.cstyle[y], self.text[y], self.dots[y], self.dstyle[y]
        for x in range(self.w):
            if text[x]:
                ch, st = chars[x], cstyle[x]
            elif dots[x]:
                ch, st = chr(0x2800 + dots[x]), dstyle[x]
            else:
                ch, st = " ", cstyle[x]
            if st != rstyle and run:
                yield "".join(run), rstyle
                run = []
            rstyle = st
            run.append(ch)
        if run:
            yield "".join(run), rstyle

    def strips(self) -> list[Strip]:
        """One Textual Strip per row, built directly (much cheaper than Text -> Content -> Strip every refresh)."""
        return [Strip([Segment(t, _style(st)) for t, st in self._runs(y)], self.w) for y in range(self.h)]

    def render(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for y in range(self.h):
            run, rstyle = [], None
            for x in range(self.w):
                if self.text[y][x]:
                    ch, st = self.chars[y][x], self.cstyle[y][x]
                elif self.dots[y][x]:
                    ch, st = chr(0x2800 + self.dots[y][x]), self.dstyle[y][x]
                else:
                    ch, st = " ", self.cstyle[y][x]
                if st != rstyle and run:
                    out.append("".join(run), rstyle or None)
                    run = []
                rstyle = st
                run.append(ch)
            if run:
                out.append("".join(run), rstyle or None)
            if y < self.h - 1:
                out.append("\n")
        return out


@lru_cache(maxsize=512)
def _style(spec: str | None) -> Style:
    return Style.parse(spec) if spec else Style.null()
