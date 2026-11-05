"""Pixel art drawing kit: palette, ASCII sprites, pixel font, dithering, round mask.

Everything works on small palette ("P" mode is not needed) RGB canvases of ``SIZE`` x ``SIZE``
art pixels and is scaled up with nearest neighbour at the end, so edges stay crisp.
"""

from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageDraw

PANEL = 240         # the GC9A01 panel
SIZE = 80           # art pixels across the circle for the chunky 3x sprites
SCALE = PANEL // SIZE
LOFI_SIZE = 120     # the lofi canal scene is drawn at 2x (see lofi.py)

# One palette for every scene. Sprite strings use these letters; "." is transparent.
# Navy, copper, green, amber and red are the same colours as the status screen.
PALETTE: dict[str, tuple[int, int, int]] = {
    "K": (14, 12, 24),      # outline / near black
    "N": (10, 20, 40),      # navy (status screen background)
    "n": (24, 40, 76),      # night blue
    "b": (44, 84, 140),     # river blue
    "c": (98, 160, 214),    # day sky
    "s": (170, 214, 240),   # pale sky / foam
    "W": (244, 244, 240),   # white
    "g": (196, 198, 210),   # light grey (fur shade)
    "G": (112, 114, 132),   # grey
    "P": (244, 150, 170),   # pink (ears, nose)
    "C": (200, 120, 50),    # copper
    "O": (232, 168, 84),    # light copper / straw
    "B": (130, 76, 42),     # wood
    "D": (74, 42, 26),      # dark wood
    "E": (33, 209, 159),    # green (status ok)
    "e": (22, 120, 84),     # dark green / grass shade
    "L": (130, 220, 90),    # light grass
    "Y": (250, 204, 21),    # amber (status degraded)
    "R": (239, 68, 68),     # red (status offline)
    "M": (255, 244, 196),   # moon
    # cyberpunk neons
    "F": (255, 46, 151),    # hot magenta
    "Q": (0, 236, 255),     # electric cyan
    "V": (140, 64, 230),    # violet
    "U": (44, 18, 74),      # deep purple
    "u": (20, 8, 36),       # night purple
    "H": (26, 168, 88),     # mallard green
    # lofi canal (muted teals, wet asphalt, neon)
    "a": (9, 20, 32),       # deepest teal / sky at the top
    "d": (16, 38, 56),      # dark teal (far towers, shadow faces)
    "f": (26, 62, 84),      # mid teal (buildings)
    "h": (44, 96, 120),     # light teal (lit edges, fog)
    "i": (84, 150, 170),    # pale teal (rain, highlights)
    "j": (30, 40, 66),      # slate (coat, car cabin)
    "k": (54, 70, 104),     # light slate (coat lit side, car body)
    "l": (255, 120, 200),   # soft pink neon
    "m": (150, 90, 220),    # lavender neon
    "o": (120, 230, 200),   # mint neon
    "p": (12, 70, 50),      # dark mallard green
    "r": (36, 34, 50),      # wet asphalt
    "t": (58, 56, 80),      # wet asphalt, lit
    "w": (200, 70, 70),     # tail light red
    "x": (120, 130, 150),   # shearling collar / concrete
    "z": (18, 16, 28),      # trousers / deep shadow
    "y": (34, 30, 48),      # trousers, lit
    "v": (34, 48, 44),      # canal water
    "A": (58, 78, 70),      # canal water, lit
    "T": (255, 235, 150),   # warm window light
}

Color = tuple[int, int, int]


def c(key: str) -> Color:
    return PALETTE[key]


def canvas(fill: str = "N") -> Image.Image:
    return Image.new("RGB", (SIZE, SIZE), c(fill))


class Sprite:
    """An ASCII sprite: one string per row, one palette letter per pixel, "." transparent."""

    def __init__(self, rows: str | list[str]) -> None:
        if isinstance(rows, str):
            rows = [r for r in rows.strip("\n").splitlines()]
        rows = [r.strip() for r in rows if r.strip()]
        self.rows = rows
        self.w = max(len(r) for r in rows)
        self.h = len(rows)
        bad = {ch for r in rows for ch in r} - set(PALETTE) - {"."}
        if bad:
            raise ValueError(f"unknown sprite colours {sorted(bad)}")

    def flip(self) -> Sprite:
        return Sprite([r.ljust(self.w, ".")[::-1] for r in self.rows])

    def replace(self, old: str, new: str) -> Sprite:
        return Sprite([r.replace(old, new) for r in self.rows])

    def draw(self, img: Image.Image, x: int, y: int, scale: int = 1) -> None:
        px = img.load()
        for j, row in enumerate(self.rows):
            for i, ch in enumerate(row):
                if ch == ".":
                    continue
                for sy in range(scale):
                    for sx in range(scale):
                        xx, yy = x + i * scale + sx, y + j * scale + sy
                        if 0 <= xx < img.width and 0 <= yy < img.height:
                            px[xx, yy] = PALETTE[ch]


def stamp(img: Image.Image, sprite: Sprite, x: int, y: int, flip: bool = False, scale: int = 1) -> None:
    (sprite.flip() if flip else sprite).draw(img, x, y, scale)


def shadow(img: Image.Image, x: int, y: int, w: int, color: str = "e") -> None:
    """A flat oval shadow, ``w`` wide, centred under (x + w/2, y)."""
    for k in range(w):
        put(img, x + k, y, color)
    for k in range(2, w - 2):
        put(img, x + k, y + 1, color)


# ---------------------------------------------------------------- 3x5 pixel font
_FONT = {
    "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110",
    "E": "111100110100111", "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101", "L": "100100100100111",
    "M": "101111111101101", "N": "110101101101101", "O": "010101101101010", "P": "110101110100100",
    "Q": "010101101110011", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101111", "V": "101101101101010", "W": "101101101111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111",
    "0": "111101101101111", "1": "010110010010111", "2": "110001010100111", "3": "110001010001110",
    "4": "101101111001001", "5": "111100110001110", "6": "011100111101111", "7": "111001010010010",
    "8": "111101111101111", "9": "111101111001110",
    " ": "000000000000000", "!": "010010010000010", ".": "000000000000010", ":": "000010000010000",
    "-": "000000111000000", "?": "110001010000010", "'": "010010000000000", "/": "001001010100100",
}


def text_width(s: str, scale: int = 1) -> int:
    return (len(s) * 4 - 1) * scale if s else 0


def text(img: Image.Image, s: str, x: int, y: int, color: str = "W", scale: int = 1, shadow: str | None = None) -> None:
    """Draw ``s`` in the 3x5 font; ``x`` may be "center"."""
    if shadow:
        text(img, s, x if x == "center" else x + 1, y + 1, shadow, scale)
    if x == "center":
        x = (img.width - text_width(s, scale)) // 2
    d = ImageDraw.Draw(img)
    for k, ch in enumerate(s.upper()):
        bits = _FONT.get(ch, _FONT["?"])
        ox = x + k * 4 * scale
        for j in range(5):
            for i in range(3):
                if bits[j * 3 + i] == "1":
                    d.rectangle((ox + i * scale, y + j * scale, ox + i * scale + scale - 1, y + j * scale + scale - 1), fill=c(color))


# ---------------------------------------------------------------- fills
BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def dither_rect(img: Image.Image, box: tuple[int, int, int, int], a: str, b: str, amount: float) -> None:
    """Fill ``box`` with a 4x4 Bayer mix of colours ``a`` and ``b``; ``amount`` 0 = all a, 1 = all b."""
    px = img.load()
    x0, y0, x1, y1 = box
    for y in range(max(0, y0), min(img.height, y1)):
        for x in range(max(0, x0), min(img.width, x1)):
            px[x, y] = c(b) if BAYER[y % 4][x % 4] < amount * 16 else c(a)


def vgradient(img: Image.Image, y0: int, y1: int, a: str, b: str) -> None:
    """Dithered vertical gradient from ``a`` (top) to ``b`` (bottom), the 8-bit way."""
    for y in range(y0, y1):
        dither_rect(img, (0, y, img.width, y + 1), a, b, (y - y0) / max(1, y1 - y0 - 1))


def rect(img: Image.Image, box: tuple[int, int, int, int], color: str) -> None:
    ImageDraw.Draw(img).rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=c(color))


def put(img: Image.Image, x: int, y: int, color: str) -> None:
    if 0 <= x < img.width and 0 <= y < img.height:
        img.putpixel((x, y), c(color))


# ---------------------------------------------------------------- round panel
@lru_cache(maxsize=4)
def _circle(size: int, inset: float) -> list[list[bool]]:
    r = size / 2 - inset
    cx = cy = size / 2
    return [[math.hypot(x + 0.5 - cx, y + 0.5 - cy) <= r for x in range(size)] for y in range(size)]


def ring(img: Image.Image, color: str, width: int = 2, frac: float = 1.0, start_deg: float = -90.0) -> None:
    """Pixel ring just inside the circle edge; ``frac`` < 1 draws an arc clockwise from 12 o'clock."""
    px = img.load()
    size = img.width
    r_out, r_in = size / 2, size / 2 - width
    for y in range(size):
        for x in range(size):
            dx, dy = x + 0.5 - size / 2, y + 0.5 - size / 2
            d = math.hypot(dx, dy)
            if r_in <= d <= r_out:
                ang = (math.degrees(math.atan2(dy, dx)) - start_deg) % 360
                if ang <= frac * 360:
                    px[x, y] = c(color)


def finish(img: Image.Image, scale: int | None = None) -> Image.Image:
    """Mask to the round panel (outside the circle goes black) and scale up to 240x240.

    The scale follows the canvas: an 80 px canvas is scaled 3x, a 120 px canvas 2x.
    """
    scale = scale or PANEL // img.width
    mask = _circle(img.width, 0.0)
    out = img.copy()
    px = out.load()
    for y in range(img.height):
        for x in range(img.width):
            if not mask[y][x]:
                px[x, y] = (0, 0, 0)
    return out.resize((img.width * scale, img.height * scale), Image.NEAREST)
