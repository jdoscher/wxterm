"""Big digits for the temperature, weather-code names, compass point names.

Big digit styles (WXTERM_BIGFONT, or `f` on the keyboard), default first:
  segment  seven-segment digits drawn with heavy box-drawing lines, like a VFD clock
  dots     a real font (DejaVu Sans Condensed Bold) rasterised into braille: a smooth dot-matrix/LED look
  blocks   the original 3x5 pixel blocks
All are 5 rows tall so they drop into the same space.
"""
from __future__ import annotations

import functools
import os

STYLES = ("segment", "dots", "blocks")

# --- blocks: the original 3x5 pixel font ------------------------------------------------------------
FONT = {
    "0": ["###", "#.#", "#.#", "#.#", "###"], "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"], "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"], "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"], "7": ["###", "..#", "..#", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"], "9": ["###", "#.#", "###", "..#", "###"],
    ":": [".", "#", ".", "#", "."], "-": ["...", "...", "###", "...", "..."],
    "°": ["##", "##", "..", "..", ".."], " ": [".", ".", ".", ".", "."], "?": ["###", "..#", ".##", "...", ".#."],
}


def _blocks(s: str) -> list[str]:
    rows = [""] * 5
    for i, ch in enumerate(s):
        g = FONT.get(ch, FONT["?"])
        for r in range(5):
            rows[r] += "".join("██" if c == "#" else "  " for c in g[r]) + (" " if i < len(s) - 1 else "")
    return rows


# --- segment: seven-segment line drawing ------------------------------------------------------------
SEG = {
    "0": ["┏━━━┓", "┃   ┃", "┃   ┃", "┃   ┃", "┗━━━┛"],
    "1": ["    ╻", "    ┃", "    ┃", "    ┃", "    ╹"],
    "2": ["╺━━━┓", "    ┃", "┏━━━┛", "┃    ", "┗━━━╸"],
    "3": ["╺━━━┓", "    ┃", " ━━━┫", "    ┃", "╺━━━┛"],
    "4": ["╻   ╻", "┃   ┃", "┗━━━┫", "    ┃", "    ╹"],
    "5": ["┏━━━╸", "┃    ", "┗━━━┓", "    ┃", "╺━━━┛"],
    "6": ["┏━━━╸", "┃    ", "┣━━━┓", "┃   ┃", "┗━━━┛"],
    "7": ["╺━━━┓", "    ┃", "    ┃", "    ┃", "    ╹"],
    "8": ["┏━━━┓", "┃   ┃", "┣━━━┫", "┃   ┃", "┗━━━┛"],
    "9": ["┏━━━┓", "┃   ┃", "┗━━━┫", "    ┃", "╺━━━┛"],
    "-": ["     ", "     ", "╺━━━╸", "     ", "     "],
    "°": ["┏━┓", "┗━┛", "   ", "   ", "   "],
    " ": ["  ", "  ", "  ", "  ", "  "],
    "?": ["╺━━━┓", "    ┃", "  ━━┛", "     ", "  ╹  "],
}


def _segment(s: str) -> list[str]:
    rows = [""] * 5
    for i, ch in enumerate(s):
        g = SEG.get(ch, SEG["?"])
        for r in range(5):
            rows[r] += g[r] + (" " if i < len(s) - 1 else "")
    return rows


# --- dots: TrueType digits rasterised to braille (2x4 dots per cell, 5 rows = 20 dots tall) ----------
FONT_PATHS = [
    os.environ.get("WXTERM_BIGFONT_FILE", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


@functools.lru_cache(maxsize=64)
def _dots(s: str) -> tuple[str, ...] | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    path = next((p for p in FONT_PATHS if p and os.path.exists(p)), None)
    if not path:
        return None
    H = 20  # 5 rows x 4 dots
    # size the font so a digit's ink is exactly H dots tall
    size = 30
    for _ in range(12):
        f = ImageFont.truetype(path, size)
        bb0 = f.getbbox("0")
        ink = int(bb0[3]) - int(bb0[1])
        if ink == H:
            break
        size = max(8, round(size * H / max(ink, 1)))
    f = ImageFont.truetype(path, size)
    top = int(f.getbbox("0")[1])
    # the degree sign sits at cap height on its own, slightly smaller
    parts, x = [], 0
    for ch in s:
        if ch == "-":
            ch = "−"  # a real minus sits at digit mid-height; the hyphen is short and low
        if ch == " ":
            x += 4
            continue
        bb = [int(v) for v in f.getbbox(ch)]
        parts.append((ch, x - bb[0]))
        x += (bb[2] - bb[0]) + 3
    W = int(max(2, x - 1 + (x - 1) % 2))
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    for ch, px in parts:
        d.text((px, -top), ch, font=f, fill=255)
    px = img.load()
    assert px is not None
    rows = []
    for cy in range(H // 4):
        line = []
        for cx in range(W // 2):
            b = 0
            for dy in range(4):
                for dx in range(2):
                    if int(px[cx * 2 + dx, cy * 4 + dy]) >= 110:  # type: ignore[arg-type]
                        b |= _BITS[dy][dx]
            line.append(chr(0x2800 + b) if b else " ")
        rows.append("".join(line))
    return tuple(rows)


def big(s: str, style: str = "segment") -> list[str]:
    if style == "dots":
        r = _dots(s)
        if r:
            return list(r)
        style = "segment"  # no Pillow or no font file: fall back to line drawing
    return _segment(s) if style == "segment" else _blocks(s)


def big_width(s: str, style: str = "segment") -> int:
    return len(big(s, style)[0])


WMO = {0: "CLEAR", 1: "MAINLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST", 45: "FOG", 48: "RIME FOG",
       51: "LIGHT DRIZZLE", 53: "DRIZZLE", 55: "HEAVY DRIZZLE", 61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
       71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 80: "SHOWERS", 81: "SHOWERS", 82: "HEAVY SHOWERS",
       95: "THUNDERSTORM", 96: "T-STORM + HAIL", 99: "T-STORM + HAIL"}

DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_name(deg) -> str:
    return "" if deg is None else DIRS[int((deg % 360) / 22.5 + 0.5) % 16]
