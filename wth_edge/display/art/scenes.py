"""The bundled animations, drawn in code with the pixel kit.

The theme is a neon cyberpunk canal at night: a synthwave sun behind a skyline of lit
windows, rain, a bascule bridge, the hutch on stilts with neon trim, and a mallard drake
paddling in the water.

Each scene is a function returning ``(frames, durations_ms, loop)``. Frames are 80x80 art
canvases; ``build`` masks them to the circle, adds CRT scanlines and scales them to
240x240 GIFs.

To add a scene: write a function here, add it to ``SCENES``, run ``wth-edge art build``.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

from .kit import (
    PALETTE,
    SIZE,
    Sprite,
    c,
    canvas,
    finish,
    put,
    rect,
    ring,
    stamp,
    text,
    vgradient,
)

GIF_DIR = Path(__file__).parent / "gifs"
SCANLINE_DIM = 0.55

# ---------------------------------------------------------------- sprites
# Mallard drake facing right, sitting on the water: green head, white collar, brown chest,
# grey body, blue-violet speculum, black tail curl, and a wraparound cyan visor with a
# magenta tip. The bottom row is the waterline.
MALLARD = Sprite("""
..............KKKK....
.............KHHHHK...
............KKQQQQQFK.
............KHHHHHHKKK
............KHHHHHHYYYK
.............KHHHHKYYK
..............KWWWK...
.K.....KKKKKKKBBBBK...
KWK..KKggggggggBBBBK..
KKKKKgggggggggggBBBBK.
.KKggGGGGGGggggggBBBK.
..KggVVbbGGGggggggggK.
...KgggggggggggggggK..
....KKKKKKKKKKKKKKK...
""")
# "Blink": the visor dims for a beat.
MALLARD_BLINK = Sprite(MALLARD.rows[:2] + ["............KKVVVVVQK."] + MALLARD.rows[3:])
VISOR_ROW, VISOR_X0, VISOR_X1 = 2, 14, 19   # lens pixels in MALLARD, for the moving glint
# Quack: bill open.
MALLARD_QUACK = Sprite(MALLARD.rows[:3] + [
    "............KHHHHHHKKKK",
    "............KHHHHHHK...",
    ".............KHHHHKYYYK",
] + MALLARD.rows[6:])
# Head down for a dabble: the head drops behind the chest, tail tips up.
MALLARD_DABBLE = Sprite("""
.KK...................
KWWK..................
KKKKK.................
..KKKKK...............
....KKggKKKKKKK.......
.....KggggggggBBK.....
......KgggggggggBBKQQK
......KGGGGGggggggBHHK
.......KVVbbGGgggggKYYK
........KggggggggggK.K
.........KKKKKKKKKK...
""")
WING = Sprite("""
....KK
...KggK
..KgggK
.KgGgK.
KgGVK..
KKKK...
""")
MALLARD_SLEEP = Sprite("""
.........KKKKK........
........KVVVVVK.......
.K.....KHHKKHHHK......
KWK..KKKHHHHHHHKK.....
KKKKKggKKWWWWWKBBK....
.KKggGGGGGGggggBBBK...
..KggVVbbGGGggggggBK..
...KgggggggggggggggK..
....KKKKKKKKKKKKKKK...
""")
MALLARD_FACE = Sprite("""
.......KKKKKK.......
.....KKHHHHHHKK.....
....KHHHHHHHHHHK....
...KHHHHHHHHHHHHK...
..KKKKKKKKKKKKKKKK..
..KQQQQQQQQQQQQQFK..
..KKQQQQQQQQQQQQKK..
...KHHHHKKKKHHHHK...
...KHHHKYYYYKHHHK...
...KHHKYYYYYYKHHK...
....KHKYYOOYYKHK....
.....KKYYYYYYKK.....
......KKKKKKKK......
.....KWWWWWWWWK.....
....KBBBBBBBBBBK....
...KBBBBBBBBBBBBK...
""")
MALLARD_FACE_BLINK = Sprite(MALLARD_FACE.rows[:5] + ["..KVVVVVVVVVVVVVQK..", "..KKVVVVVVVVVVVVKK.."] + MALLARD_FACE.rows[7:])
FACE_VISOR_ROW, FACE_VISOR_X0, FACE_VISOR_X1 = 5, 3, 16

BOAT = Sprite("""
.....Q.....
.....QF....
.....QFF...
.....QFFF..
KKKKKKKKKKK
KQQQQQQQQK.
.KuuuuuuK..
""")

Z = Sprite("""
QQQ
..Q
.Q.
Q..
QQQ
""")

# ---------------------------------------------------------------- props
WATER_Y = 44
DUCK_X, DUCK_WATERLINE = 40, 64


def sun_colours(heat: float | None, night: bool) -> tuple[str, str]:
    """(outer, core) sun colours. ``heat`` 0 = cool Pi, 1 = hot; None = the plain look."""
    if heat is None:
        return ("V", "F") if night else ("F", "Y")
    if heat < 0.4:
        return ("V", "Q") if night else ("F", "Y")
    if heat < 0.75:
        return "F", "O"
    return "R", "R" if heat >= 0.9 else "O"


def sky(img: Image.Image, t: int, night: float = 0.0, heat: float | None = None) -> None:
    """Purple sky with a striped synthwave sun; ``night`` dims it, ``heat`` reddens it."""
    vgradient(img, 0, WATER_Y, "u", "U" if night < 0.5 else "u")
    d = ImageDraw.Draw(img)
    sun, core = sun_colours(heat, night >= 0.5)
    if heat is not None and heat >= 0.9 and t % 4 < 2:  # overheating: the sun pulses
        sun, core = "F", "R"
    d.ellipse((26, 10, 54, 38), fill=c(sun))
    d.ellipse((30, 10, 50, 26), fill=c(core))
    # stripes cut out of the lower half, scrolling down slowly
    for k in range(5):
        y = 26 + k * 3 + (t // 3) % 3
        if y < 38:
            rect(img, (24, y, 56, y + 1 + k // 2), "U" if night < 0.5 else "u")
    for k, (x, y) in enumerate(((8, 8), (18, 4), (62, 6), (70, 14), (12, 18), (58, 2))):
        if (t + k * 5) % 14 < 10:
            put(img, x, y, "W" if k % 2 else "Q")


_BUILDINGS = [  # x0, x1, top
    (0, 9, 22), (9, 16, 30), (16, 24, 16), (24, 30, 32), (50, 56, 26), (56, 64, 12), (64, 71, 28), (71, 80, 20),
]


def skyline(img: Image.Image, t: int, lights_on: float = 0.6, brownout: bool = False, rec: bool = False) -> None:
    """Towers with lit windows; ``brownout`` (low power) makes the whole city stutter."""
    rng = random.Random(7)
    dark = brownout and (t % 5 in (1, 3) or t % 11 == 0)
    for x0, x1, top in _BUILDINGS:
        rect(img, (x0, top, x1, WATER_Y), "K")
        put(img, x0, top, "V")
        for wy in range(top + 2, WATER_Y - 1, 3):
            for wx in range(x0 + 1, x1 - 1, 2):
                r = rng.random()
                flicker = rng.random() < 0.08 and (t // 2 + wx) % 5 == 0
                if brownout and (dark or (wx + wy + t) % 3 == 0):
                    continue
                if r < lights_on and not flicker:
                    put(img, wx, wy, "Q" if r < 0.25 else "F" if r < 0.42 else "Y" if r < 0.5 else "V")
    # vertical neon sign on the tall right tower, buzzing now and then
    if not (t % 23 in (5, 7)):
        for k, ch in enumerate("WTH"):
            text(img, ch, 57, 14 + k * 6, "F")
    # antenna light
    put(img, 60, 10, "R" if t % 8 < 4 else "K")
    put(img, 60, 11, "G")
    if rec and t % 8 < 5:  # recording: a red dot beside the sign
        rect(img, (52, 15, 55, 18), "R")


def bridge(img: Image.Image, lift: float = 0.0, lamps: str = "F") -> None:
    """A little bascule bridge across the canal; ``lift`` 0 = down, 1 = fully open."""
    left, right, deck_y = 34, 76, WATER_Y
    for x in (left, right):
        rect(img, (x - 1, 37, x + 2, deck_y + 3), "u")
        rect(img, (x - 1, 36, x + 2, 37), lamps)
    span = (right - left) / 2
    ang = math.radians(75 * lift)
    d = ImageDraw.Draw(img)
    for pivot, sign in ((left + 1, 1), (right, -1)):
        x2 = pivot + sign * span * math.cos(ang)
        y2 = deck_y - span * math.sin(ang)
        d.line((pivot, deck_y, x2, y2), fill=c("Q"), width=1)
        d.line((pivot, deck_y + 1, x2, y2 + 1), fill=c("V"), width=1)


def water(img: Image.Image, t: int, glow: float = 1.0) -> None:
    """Dark canal with wobbling neon reflections of the skyline and the sun."""
    vgradient(img, WATER_Y, SIZE, "U", "u")
    px = img.load()
    for y in range(WATER_Y + 1, SIZE, 2):
        wob = int(2 * math.sin((y + t) / 2.0))
        for x in range(SIZE):
            src = px[(x + wob) % SIZE, WATER_Y - 1 - (y - WATER_Y) // 2]
            # reflections fade out before the duck's lane so it stays readable
            if src not in (c("K"), c("u"), c("U")) and (x + y + t) % 3 != 0 and (y - WATER_Y) < 14 * glow:
                px[x, y] = src
    for k in range(6):  # foam highlights drifting across
        x = (k * 17 + t) % SIZE
        y = WATER_Y + 4 + k * 5
        put(img, x, y, "s")
        put(img, x + 1, y, "Q")


def rain(img: Image.Image, t: int, n: int = 26, color: str = "Q") -> None:
    rng = random.Random(3)
    for _ in range(n):
        x0, y0, sp = rng.randrange(SIZE), rng.randrange(SIZE), rng.choice((2, 3))
        y = (y0 + t * sp) % SIZE
        x = (x0 - (t * sp) // 3) % SIZE
        put(img, x, y, color)
        put(img, x - 1, y + 1, "V")


def hutch(img: Image.Image, t: int, glow: str = "Q") -> None:
    """The hutch on stilts over the water, wood under neon trim."""
    x0, x1, top, base = 6, 30, 36, 54
    d = ImageDraw.Draw(img)
    for x in (x0 + 2, x1 - 4):  # stilts into the water
        rect(img, (x, base, x + 2, 64), "D")
        put(img, x, 64, "Q")
    rect(img, (x0 - 2, base, x1 + 2, base + 2), "D")  # deck
    rect(img, (x0, top, x1, base), "B")
    for y in range(top + 3, base, 4):
        rect(img, (x0, y, x1, y + 1), "D")
    d.rectangle((x0, top, x1 - 1, base - 1), outline=c("F"))
    # glowing mesh window
    wx0, wx1, wy0, wy1 = x0 + 3, x0 + 12, top + 3, top + 12
    rect(img, (wx0, wy0, wx1, wy1), glow)
    for x in range(wx0, wx1, 2):
        for y in range(wy0, wy1, 2):
            put(img, x, y, "u")
    d.rectangle((wx0 - 1, wy0 - 1, wx1, wy1), outline=c("K"))
    # door
    dx0, dx1 = x1 - 9, x1 - 3
    rect(img, (dx0, top + 5, dx1, base), "u")
    d.rectangle((dx0, top + 5, dx1 - 1, base - 1), outline=c("V"))
    put(img, dx1 - 2, top + 11, "Y")
    # roof with a neon edge
    apex = ((x0 + x1) // 2, top - 10)
    d.polygon([(x0 - 3, top), (x1 + 2, top), apex], fill=c("u"))
    d.line([(x0 - 3, top), apex, (x1 + 2, top)], fill=c("Q"))
    # tiny "HUTCH" plate under the roof, flickers rarely
    if t % 31 != 3:
        rect(img, (x0 + 13, top + 1, x0 + 21, top + 4), "K")
        for k in range(4):
            put(img, x0 + 14 + 2 * k, top + 2, "F")


def ripples(img: Image.Image, x: int, y: int, w: int, t: int) -> None:
    spread = (t // 2) % 4
    for k in range(w):
        if (k + t) % 3:
            put(img, x + k, y + 1, "Q")
    put(img, x - 2 - spread, y + 2, "V")
    put(img, x + w + 1 + spread, y + 2, "V")
    put(img, x - 3 - spread, y + 2, "V")
    put(img, x + w + 2 + spread, y + 2, "V")


def glitch(img: Image.Image, seed: int, bands: int = 3) -> Image.Image:
    """Shift a few horizontal bands sideways, with a magenta/cyan fringe: a VHS glitch."""
    rng = random.Random(seed)
    out = img.copy()
    for _ in range(bands):
        y0 = rng.randrange(0, SIZE - 4)
        h = rng.randint(1, 4)
        dx = rng.choice((-4, -3, -2, 2, 3, 4))
        band = img.crop((0, y0, SIZE, y0 + h))
        out.paste(band, (dx, y0))
        for y in range(y0, y0 + h):
            put(out, 0 if dx > 0 else SIZE - 1, y, "F")
            put(out, dx - 1 if dx > 0 else SIZE + dx, y, "Q")
    return out


def canal_scene(t: int, lift: float = 0.0, on_water=None, night: bool = False, heat: float | None = None,
                brownout: bool = False, rec: bool = False, lights_on: float | None = None) -> Image.Image:
    img = canvas("u")
    sky(img, t, night=1.0 if night else 0.0, heat=heat)
    skyline(img, t, lights_on=(0.25 if night else 0.6) if lights_on is None else lights_on, brownout=brownout, rec=rec)
    water(img, t, glow=0.5 if night else 1.0)
    bridge(img, lift, lamps="V" if night else "F")
    if on_water:
        on_water(img)
    hutch(img, t, glow="V" if night else "Q")
    return img


def visor_off(sprite: Sprite) -> Sprite:
    """The visor with no signal (camera down): a dark lens."""
    rows = list(sprite.rows)
    if sprite.h == MALLARD.h:
        rows[VISOR_ROW] = rows[VISOR_ROW].replace("Q", "K").replace("F", "G").replace("V", "K")
    return Sprite(rows)


def visor_static(img: Image.Image, x: int, y: int, t: int) -> None:
    """Grey and red static on a dead visor lens."""
    for k in range(VISOR_X0, VISOR_X1):
        if (k * 7 + t * 3) % 5 < 2:
            put(img, x + k, y + VISOR_ROW, "R" if (k + t) % 7 == 0 else "G")


def _duck(img: Image.Image, sprite: Sprite, t: int, bob: int = 0, x: int = DUCK_X, camera_ok: bool = True) -> None:
    y = DUCK_WATERLINE - sprite.h + 1 + bob
    if not camera_ok:
        stamp(img, visor_off(sprite), x, y)
        if sprite.h == MALLARD.h:
            visor_static(img, x, y, t)
        ripples(img, x + 3, DUCK_WATERLINE, sprite.w - 6, t)
        return
    stamp(img, sprite, x, y)
    if sprite is MALLARD or (sprite.h == MALLARD.h and sprite is not MALLARD_BLINK):
        k = VISOR_X0 + t % 16  # a white glint sweeps across the lens now and then
        if k < VISOR_X1:
            put(img, x + k, y + VISOR_ROW, "W")
    ripples(img, x + 3, DUCK_WATERLINE, sprite.w - 6, t)


# ---------------------------------------------------------------- scenes
def boot():
    """Cyan ring draws in, the mallard rises, the title types out in neon, a glitch, a visor glint."""
    frames, durs = [], []
    face_x, face_y = 20, 4   # drawn at 2x: 40x32 art pixels
    shelf = 40

    def base(ring_frac: float) -> Image.Image:
        img = canvas("u")
        for y in range(0, SIZE, 4):  # faint grid floor, the retro-future way
            rect(img, (0, y, SIZE, y + 1), "U")
        if ring_frac > 0:
            ring(img, "Q", 2, ring_frac)
            ring(img, "F", 1, ring_frac * 0.9)
        return img

    def with_face(img: Image.Image, dy: int = 0, blink: bool = False) -> Image.Image:
        stamp(img, MALLARD_FACE_BLINK if blink else MALLARD_FACE, face_x, face_y + dy, scale=2)
        rect(img, (0, shelf, SIZE, SIZE), "u")   # the mallard rises from behind the title
        for x in range(0, SIZE, 2):
            put(img, x, shelf, "F")
        ring(img, "Q", 2)
        ring(img, "F", 1)
        return img

    for k in range(1, 13):
        frames.append(base(k / 12))
        durs.append(50)
    for k in range(10):
        rise = round(36 * (1 - (k + 1) / 10) ** 2) - (2 if k == 8 else 0)  # ease out, small bounce
        frames.append(with_face(base(1), rise))
        durs.append(50)
    title = ["WATCH THE", "HUTCH"]
    x_a = (SIZE - (len(title[0]) * 4 - 1)) // 2
    x_b = (SIZE - (len(title[1]) * 8 - 2)) // 2
    for k in range(1, len(title[0]) + len(title[1]) + 1):
        img = with_face(base(1))
        a = title[0][:k]
        b = title[1][: max(0, k - len(title[0]))]
        text(img, a, x_a, 45, "Q")
        text(img, b, x_b - 1, 53, "Q", scale=2)   # chromatic fringe
        text(img, b, x_b, 53, "F", scale=2)
        frames.append(img)
        durs.append(60)
    final = frames[-1]
    blink = final.copy()
    stamp(blink, MALLARD_FACE_BLINK, face_x, face_y, scale=2)
    sweep = []
    for k in range(FACE_VISOR_X0, FACE_VISOR_X1, 2):  # visor glint sweeps left to right
        fr = final.copy()
        for dy in range(2):
            for dx in range(4):
                put(fr, face_x + k * 2 + dx - dy, face_y + FACE_VISOR_ROW * 2 + dy, "W")
        sweep.append(fr)
    frames += [final, glitch(final, 1, 4), glitch(final, 2, 2), final, *sweep, final, blink, final]
    durs += [400, 70, 70, 250, *[40] * len(sweep), 300, 140, 900]
    return frames, durs, False


def idle():
    """Rainy neon night: the mallard bobs, blinks, quacks and dabbles; windows flicker."""
    frames, durs = [], []
    n = 48
    for t in range(n):
        img = canal_scene(t)
        sprite = MALLARD
        if t in (10, 11, 34):
            sprite = MALLARD_BLINK
        elif t in (18, 20):
            sprite = MALLARD_QUACK
        elif 38 <= t <= 43:
            sprite = MALLARD_DABBLE
        bob = 1 if (t // 4) % 2 else 0
        _duck(img, sprite, t, bob)
        if t in (18, 20):
            text(img, "Q!", DUCK_X + 22, 42, "Y")
        rain(img, t)
        ring(img, "Q", 2)
        if t == 29:
            img = glitch(img, t, 2)
        frames.append(img)
        durs.append(125)
    return frames, durs, True


def alert():
    """Bridge opening: magenta/amber ring flashes, the mallard flaps, a neon boat goes through."""
    frames, durs = [], []
    n = 32
    for t in range(n):
        # the boat crosses left to right, behind the hutch, under the open bridge
        bx = -12 + t * (SIZE + 12) // n
        img = canal_scene(t, lift=1.0, on_water=lambda im, bx=bx: stamp(im, BOAT, bx, 41))
        if (t // 2) % 2 == 0:
            for x in (34, 76):
                rect(img, (x - 1, 35, x + 2, 37), "Y")
        hop = (0, 2, 3, 2)[t % 4] if t % 8 < 4 else 0
        _duck(img, MALLARD, t, -hop, x=DUCK_X + 2)
        if t % 2 == 0:  # wing flap
            stamp(img, WING, DUCK_X + 2 + 8, DUCK_WATERLINE - MALLARD.h - 3 - hop)
        # bouncing "!" in a speech bubble
        bob = 1 if (t // 3) % 2 else 0
        bx0, by0 = DUCK_X + 22, 36 + bob - hop
        rect(img, (bx0, by0, bx0 + 7, by0 + 9), "K")
        ImageDraw.Draw(img).rectangle((bx0, by0, bx0 + 6, by0 + 8), outline=c("F"))
        rect(img, (bx0 + 3, by0 + 2, bx0 + 4, by0 + 5), "Y")
        put(img, bx0 + 3, by0 + 6, "Y")
        put(img, bx0 + 1, by0 + 9, "F")
        rain(img, t, n=36)
        ring(img, "F" if (t // 2) % 2 == 0 else "Y", 3)
        if t % 8 == 5:
            img = glitch(img, t, 4)
        frames.append(img)
        durs.append(90)
    return frames, durs, True


def sleep():
    """Late night: most windows off, a violet sun, the mallard asleep with its head tucked, Zzz."""
    frames, durs = [], []
    n = 40
    for t in range(n):
        img = canal_scene(t // 2, night=True)
        bob = 1 if (t // 10) % 2 else 0
        _duck(img, MALLARD_SLEEP, t // 3, bob)
        for k in range(3):
            phase = (t + k * 13) % n
            zx = DUCK_X + 12 + int(4 * math.sin(phase / 5)) + phase // 6
            zy = 52 - phase
            if 6 < zy < 50:
                stamp(img, Z if phase < 26 else Z.replace("Q", "V"), zx, zy)
        rain(img, t // 2, n=12, color="V")
        ring(img, "V", 2)
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
        big = [scanlines(finish(f)) for f in frames]
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
    for rgb in PALETTE.values():
        for col in (rgb, _dim(rgb)):
            if col not in seen:
                seen.append(col)
    return seen


def _dim(rgb):
    return tuple(int(v * SCANLINE_DIM) for v in rgb)


def scanlines(img: Image.Image, every: int = 3) -> Image.Image:
    """Darken the last row of each art pixel, like a CRT. Works on the 240x240 image."""
    out = img.copy()
    px = out.load()
    for y in range(every - 1, out.height, every):
        for x in range(out.width):
            px[x, y] = _dim(px[x, y])
    return out
