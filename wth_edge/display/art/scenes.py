"""The bundled animations, drawn in code with the pixel kit.

Each scene is a function returning ``(frames, durations_ms, loop)``. Frames are 80x80 art
canvases; ``build`` masks them to the circle and scales them to 240x240 GIFs.

To add a scene: write a function here, add it to ``SCENES``, run ``wth-edge art build``.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from .kit import (
    SIZE,
    Sprite,
    c,
    canvas,
    finish,
    put,
    rect,
    ring,
    shadow,
    stamp,
    text,
    vgradient,
)

GIF_DIR = Path(__file__).parent / "gifs"

# ---------------------------------------------------------------- sprites
BUNNY_SIT = Sprite("""
..........KK.KK....
.........KWPKWPK...
.........KWPKWPK...
.........KWPKWPK...
.........KWPKWWK...
.........KWWKWK....
........KKWWWWKK...
.......KWWWWWWWWK..
......KWWWWWWKWWWK.
...KKKWWWWWWWKWWWK.
..KWWWWWWWWWWWWWPK.
.KWWWWWWWWWWWWWWK..
KgWWWWWWWWWWWKKK...
KggWWWWWWWWWWK.....
KgWWWWWWWWgWWK.....
.KWWWWWWWgWWWK.....
.KgWWWWWWgWWPK.....
..KKKKKKKKKKKK.....
""")

# Eye closed: the two eye pixels become one lash line.
BUNNY_BLINK = Sprite(BUNNY_SIT.rows[:8] + ["......KWWWWWWWWWWK.", "...KKKWWWWWWKKWWWK."] + BUNNY_SIT.rows[10:])

# Front ear flicked back.
BUNNY_FLICK = Sprite([
    "...................",
    "...................",
    "..........KK.......",
    ".........KWPK.KKK..",
    ".........KWPKKWPPK.",
    ".........KWWKWWKK..",
] + BUNNY_SIT.rows[6:])

# Nose twitch: the pink nose moves up a pixel.
BUNNY_SNIFF = Sprite(BUNNY_SIT.rows[:9] + ["...KKKWWWWWWWKWWPK.", "..KWWWWWWWWWWWWWWK."] + BUNNY_SIT.rows[11:])

BUNNY_ALERT = Sprite("""
.......KK.KK....
......KWPKWPK...
......KWPKWPK...
......KWPKWPK...
......KWPKWPK...
......KWWKWWK...
.....KKWWWWWKK..
....KWWWWWWWWWK.
....KWWWWWKWWWWK
....KWWWWWKWWWPK
....KWWWWWWWWWK.
.....KKWWWWWKK..
....KWWWWWWWK...
...KWWWWWWWWWK..
...KWWWWWWWgWK..
...KWWWWWWKWWK..
..KgWWWWWWWWK...
..KgWWWWWWWWK...
.KggWWWWWWWWK...
.KgWWWWWWWWWK...
.KgWWWWgWWWPK...
..KKKKKKKKKKK...
""")

BUNNY_LOAF = Sprite("""
.......KKKKKKKKK........
......KPPPPPPPWWK.......
.......KKKKKKKWWKK......
.....KKWWWWWWWWWKWKK....
...KKWWWWWWWWWWWWKWWK...
..KWWWWWWWWWWWWWWKWWWK..
.KWWWWWWWWWWWWWWWWWWWWK.
.KWWWWWWWWWWWWWWWKKWWWPK
KgWWWWWWWWWWWWWWWWWWWWK.
KgWWWWWWWWWWWWWWWWWWWK..
KggWWWWWWWWWWWWgWWWWK...
.KgggWWWWWWWWWWgWWWK....
..KKKKKKKKKKKKKKKKK.....
""")
# Breathing in: one extra row of fur, drawn a pixel higher.
BUNNY_LOAF_IN = Sprite(BUNNY_LOAF.rows[:6] + [BUNNY_LOAF.rows[6]] + BUNNY_LOAF.rows[6:])

BUNNY_FACE = Sprite("""
....KK......KK......
...KWPK....KWPK.....
...KWPK....KWPK.....
...KWPK....KWPK.....
...KWPK....KWPK.....
...KWPK....KWPK.....
...KWWK....KWWK.....
..KKWWWKKKKWWWKK....
.KWWWWWWWWWWWWWWK...
KWWWWWWWWWWWWWWWWK..
KWWWKKWWWWWWKKWWWK..
KWWWKKWWWWWWKKWWWK..
KWPPWWWWWWWWWWPPWK..
KWWWWWWWKPPKWWWWWK..
.KWWWWWWWKKWWWWWK...
.KWWWWWWKWWKWWWWK...
..KKWWWWWWWWWWKK....
....KKKKKKKKKK......
""")
BUNNY_FACE_BLINK = Sprite(BUNNY_FACE.rows[:10] + ["KWWWWWWWWWWWWWWWWK..", "KWWWKKWWWWWWKKWWWK.."] + BUNNY_FACE.rows[12:])

CLOUD = Sprite("""
...WWWW.....
.WWWWWWWW...
WWWWWWWWWWW.
.ssssssssss.
""")

BOAT = Sprite("""
.....K.....
.....KW....
.....KWW...
.....KWWW..
KKKKKKKKKKK
KCCCCCCCCK.
.KDDDDDDK..
""")

Z = Sprite("""
WWW
..W
.W.
W..
WWW
""")

# ---------------------------------------------------------------- props
GROUND_Y = 58


def sky_day(img: Image.Image, t: int) -> None:
    vgradient(img, 0, 46, "c", "s")
    for k, (x0, y, speed) in enumerate(((6, 12, 0.25), (50, 20, 0.15), (30, 6, 0.1))):
        x = int(x0 + t * speed) % (SIZE + 24) - 14
        stamp(img, CLOUD, x, y)


def sky_night(img: Image.Image, t: int) -> None:
    vgradient(img, 0, 46, "N", "n")
    # moon with a crater
    ImageDraw.Draw(img).ellipse((54, 9, 63, 18), fill=c("M"))
    put(img, 57, 12, "O")
    put(img, 60, 15, "O")
    stars = [(14, 14), (24, 7), (36, 12), (44, 5), (70, 26), (20, 28), (46, 22), (8, 34), (66, 6)]
    for k, (x, y) in enumerate(stars):
        phase = (t + k * 3) % 12
        if phase < 9:
            put(img, x, y, "W" if phase < 4 else "G")
        if phase < 2:  # brightest: a little cross
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                put(img, x + dx, y + dy, "G")


def river(img: Image.Image, t: int, night: bool = False) -> None:
    base, hi = ("n", "b") if night else ("b", "s")
    rect(img, (0, 46, SIZE, GROUND_Y - 4), base)
    for y in range(47, GROUND_Y - 4, 2):
        for x in range(SIZE):
            if (x + t + y * 3) % 11 == 0:
                put(img, x, y, hi)
                put(img, x + 1, y, hi)
    if night:  # moon reflection
        for y in range(47, GROUND_Y - 4):
            if (y + t) % 2 == 0:
                rect(img, (57 + (y + t) % 3 - 1, y, 61 - (y % 2), y + 1), "M")


def bridge(img: Image.Image, lift: float = 0.0, night: bool = False) -> None:
    """A little bascule bridge on the far bank; ``lift`` 0 = down, 1 = fully open."""
    steel = "G" if not night else "n"
    left, right, deck_y = 40, 76, 44
    for x in (left, right):
        rect(img, (x - 1, 37, x + 2, deck_y + 3), "K" if night else "D")
        rect(img, (x - 1, 36, x + 2, 37), "R")  # warning lamps
    span = (right - left) / 2
    ang = math.radians(75 * lift)
    d = ImageDraw.Draw(img)
    for pivot, sign in ((left + 1, 1), (right, -1)):
        x2 = pivot + sign * span * math.cos(ang)
        y2 = deck_y - span * math.sin(ang)
        d.line((pivot, deck_y, x2, y2), fill=c(steel), width=2)


def grass(img: Image.Image, t: int, night: bool = False) -> None:
    top, low = ("e", "N") if night else ("E", "e")
    rect(img, (0, GROUND_Y - 4, SIZE, GROUND_Y - 2), "e" if not night else "n")  # bank
    vgradient(img, GROUND_Y - 2, SIZE, top, low)
    for x in range(2, SIZE, 5):
        sway = 1 if ((x // 5 + t // 4) % 3 == 0) else 0
        h = 2 + (x * 7) % 3
        for k in range(h):
            put(img, x + (sway if k == h - 1 else 0), GROUND_Y - 2 - k, "L" if not night else "e")


def hutch(img: Image.Image, night: bool = False) -> None:
    x0, x1, top, base = 8, 34, 36, GROUND_Y - 3
    d = ImageDraw.Draw(img)
    # legs
    for x in (x0 + 2, x1 - 4):
        rect(img, (x, base, x + 2, GROUND_Y + 1), "D")
    # body with planks
    rect(img, (x0, top, x1, base), "B")
    for y in range(top + 3, base, 4):
        rect(img, (x0, y, x1, y + 1), "D")
    d.rectangle((x0, top, x1 - 1, base - 1), outline=c("K"))
    # mesh window
    wx0, wx1, wy0, wy1 = x0 + 3, x0 + 13, top + 3, top + 13
    rect(img, (wx0, wy0, wx1, wy1), "Y" if night else "K")
    for x in range(wx0, wx1, 2):
        for y in range(wy0, wy1, 2):
            put(img, x, y, "O" if night else "G")
    d.rectangle((wx0 - 1, wy0 - 1, wx1, wy1), outline=c("D"))
    # straw peeking out of the window
    if not night:
        for x in range(wx0, wx1, 3):
            put(img, x + 1, wy1 - 1, "O")
    # door
    dx0, dx1 = x1 - 10, x1 - 3
    rect(img, (dx0, top + 5, dx1, base), "D")
    d.rectangle((dx0, top + 5, dx1 - 1, base - 1), outline=c("K"))
    put(img, dx1 - 2, top + 11, "O")  # latch
    # roof
    d.polygon([(x0 - 3, top), (x1 + 2, top), ((x0 + x1) // 2, top - 11)], fill=c("C"), outline=c("K"))
    for y in range(top - 8, top, 3):
        half = (y - (top - 11)) * (x1 - x0 + 5) // 22
        d.line(((x0 + x1) // 2 - half + 1, y, (x0 + x1) // 2 + half - 1, y), fill=c("B"))
    # little sign
    rect(img, (x0 + 15, top + 1, x0 + 22, top + 4), "O")
    put(img, x0 + 17, top + 2, "D")
    put(img, x0 + 19, top + 2, "D")


def day_scene(t: int, lift: float = 0.0, on_river=None) -> Image.Image:
    img = canvas()
    sky_day(img, t)
    river(img, t)
    bridge(img, lift)
    if on_river:
        on_river(img)
    grass(img, t)
    hutch(img)
    return img


# ---------------------------------------------------------------- scenes
def boot():
    """Copper ring draws in, the bunny pops up, the title types out, a blink. Plays once."""
    frames, durs = [], []

    def base(ring_frac: float) -> Image.Image:
        img = canvas("N")
        if ring_frac > 0:
            ring(img, "C", 2, ring_frac)
        return img

    for k in range(1, 13):
        frames.append(base(k / 12))
        durs.append(50)
    face_x, face_y = 20, 6   # drawn at 2x: 40x36 art pixels
    shelf = 43

    def with_face(img: Image.Image, dy: int = 0, blink: bool = False) -> Image.Image:
        stamp(img, BUNNY_FACE_BLINK if blink else BUNNY_FACE, face_x, face_y + dy, scale=2)
        rect(img, (0, shelf, SIZE, SIZE), "N")   # the bunny rises from behind the title
        ring(img, "C", 2)
        return img

    for k in range(10):
        rise = round(38 * (1 - (k + 1) / 10) ** 2) - (2 if k == 8 else 0)  # ease out, small bounce
        frames.append(with_face(base(1), rise))
        durs.append(50)
    title = ["WATCH THE", "HUTCH"]
    for k in range(1, len(title[0]) + len(title[1]) + 1):
        img = with_face(base(1))
        a = title[0][:k]
        b = title[1][: max(0, k - len(title[0]))]
        x_a = (SIZE - (len(title[0]) * 4 - 1)) // 2
        x_b = (SIZE - (len(title[1]) * 8 - 2)) // 2
        text(img, a, x_a, 47, "W", shadow="K")
        text(img, b, x_b, 55, "C", scale=2, shadow="K")
        frames.append(img)
        durs.append(60)
    final = frames[-1]
    blink = final.copy()
    stamp(blink, BUNNY_FACE_BLINK, face_x, face_y, scale=2)
    frames += [final, blink, final]
    durs += [600, 140, 900]
    return frames, durs, False


def idle():
    """Daytime: clouds drift, water shimmers, the bunny blinks, sniffs and flicks an ear."""
    frames, durs = [], []
    n = 48
    for t in range(n):
        img = day_scene(t)
        ring(img, "E", 2)
        sprite = BUNNY_SIT
        if t in (12, 13, 36):
            sprite = BUNNY_BLINK
        elif 22 <= t <= 25 and t % 2 == 0:
            sprite = BUNNY_SNIFF
        elif 40 <= t <= 43:
            sprite = BUNNY_FLICK
        shadow(img, 46, GROUND_Y + 1, 15)
        stamp(img, sprite, 46, GROUND_Y - sprite.h + 1)
        frames.append(img)
        durs.append(125)
    return frames, durs, True


def alert():
    """Bridge opening: amber/red ring flashes, the bunny stands up, a boat sails through."""
    frames, durs = [], []
    n = 32
    for t in range(n):
        # the boat crosses left to right, behind the hutch, under the open bridge
        bx = -12 + t * (SIZE + 12) // n
        img = day_scene(t, lift=1.0, on_river=lambda im, bx=bx: stamp(im, BOAT, bx, 44))
        ring(img, "Y" if (t // 2) % 2 == 0 else "R", 3)
        # flashing lamps on the bridge towers
        if (t // 2) % 2 == 0:
            for x in (40, 76):
                rect(img, (x - 1, 35, x + 2, 37), "Y")
        hop = (0, 1, 2, 1)[t % 4] if t % 8 < 4 else 0
        shadow(img, 48, GROUND_Y + 1, 13)
        stamp(img, BUNNY_ALERT, 48, GROUND_Y - BUNNY_ALERT.h + 1 - hop)
        # bouncing "!" in a speech bubble
        bob = 1 if (t // 3) % 2 else 0
        bx0, by0 = 64, 22 + bob - hop
        rect(img, (bx0, by0, bx0 + 7, by0 + 9), "W")
        ImageDraw.Draw(img).rectangle((bx0, by0, bx0 + 6, by0 + 8), outline=c("K"))
        rect(img, (bx0 + 3, by0 + 2, bx0 + 4, by0 + 5), "R")
        put(img, bx0 + 3, by0 + 6, "R")
        put(img, bx0 + 1, by0 + 9, "K")
        frames.append(img)
        durs.append(90)
    return frames, durs, True


def sleep():
    """Night: stars twinkle, the hutch window glows, the bunny breathes and Zzz float up."""
    frames, durs = [], []
    n = 40
    for t in range(n):
        img = canvas()
        sky_night(img, t)
        river(img, t // 2, night=True)
        bridge(img, 0.0, night=True)
        grass(img, 0, night=True)
        hutch(img, night=True)
        ring(img, "n", 2)
        breathing_in = (t // 10) % 2 == 0
        sprite = BUNNY_LOAF_IN if breathing_in else BUNNY_LOAF
        shadow(img, 44, GROUND_Y + 2, 21, "N")
        stamp(img, sprite, 44, GROUND_Y - sprite.h + 2)
        for k in range(3):
            phase = (t + k * 13) % n
            zx = 60 + int(4 * math.sin(phase / 5)) + phase // 6
            zy = 46 - phase
            if 6 < zy < 44:
                stamp(img, Z.replace("W", "s" if phase < 26 else "G"), zx, zy)
        frames.append(img)
        durs.append(150)
    return frames, durs, True


SCENES = {"boot": boot, "idle": idle, "alert": alert, "sleep": sleep}


def build(out_dir: Path = GIF_DIR, names: list[str] | None = None) -> list[Path]:
    """Render scenes to 240x240 GIFs (plus nothing else: GIFs are both the preview and the asset)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names or list(SCENES):
        frames, durs, loop = SCENES[name]()
        big = [finish(f) for f in frames]
        path = out_dir / f"{name}.gif"
        save_gif(big, durs, path, loop)
        written.append(path)
    return written


def save_gif(frames: list[Image.Image], durs: list[int], path: Path, loop: bool) -> None:
    # Quantise every frame against one palette so colours never shimmer between frames.
    pal = Image.new("P", (1, 1))
    flat = [v for rgb in list(_unique_colours()) for v in rgb]
    pal.putpalette(flat + [0] * (768 - len(flat)))
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    kw = {"loop": 0} if loop else {}
    q[0].save(path, save_all=True, append_images=q[1:], duration=durs, disposal=1, optimize=False, **kw)


def _unique_colours():
    seen = [(0, 0, 0)]
    from .kit import PALETTE

    for rgb in PALETTE.values():
        if rgb not in seen:
            seen.append(rgb)
    return seen
