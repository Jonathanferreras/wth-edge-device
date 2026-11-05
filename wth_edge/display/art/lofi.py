"""The lofi cyberpunk canal, drawn in code at 120x120 and scaled 2x to the panel.

A muted teal night city lit by neon, rain on a wet walkway, and a drawbridge centre stage.
Officer Garrison (a mallard in a visor and a shearling-collared coat) leans on the fender of
a patrol car on the right; Eugene (a plain mallard) drifts in the canal on the left.

``draw(t, st)`` returns one frame. ``st`` holds the look (see ``DEFAULT``); the live screen
derives it from the device state in ``live.py``, the bundled GIFs use fixed values. Every
light comes from the left, so each shape has a lit edge and a shadow face.
"""

from __future__ import annotations

import math
import random

from PIL import Image, ImageDraw

from . import kit
from .kit import Sprite, put, stamp, text

C = kit.c
S = kit.LOFI_SIZE


GARRISON = Sprite("""
..........pp.pp.....
.....ppppHHpHHp.....
....pHHHHHHHHHHp....
.....ppHHHHHHHHp....
.....pHHHHHHHHpp....
....pHHHHHHHHHHp....
....pHHHHHHHHHHp....
....pKKKKKKKKKKKp...
....pQQQWQQQQQQFp...
....pQQQQQQQQQQKpYY.
.....pHHHHHHHHHpYOY.
.....pHHHHHHHHHppYp.
.....pHHHHHHHHHp....
......pWWWWWWWp.....
....jjjjkkFkkjjjj...
...jkkjjkkFkkjjjkj..
..jkkjjjjkFkkjjjjkj.
..jkkjjYjkFkkjjjjkjj
..jkkjjjjkFkkjjjjjWW
..jkkjjjjkFkkjjjkkWW
..jkkjjjjkFkkjjkkjWj
..jxxjjjjkFkkjjjjjj.
..jxxjjjjkFkkjjjjj..
..jjjjjjjkFkkjjjj...
.....jjjjkFkkjjjj...
.....jjjjkFkkjjjj...
.....jjjjkFkkjjjjj..
.....jjjjkFkkjjjjj..
.....jjjjkFkkjjjjjj.
....jjjjjkFkkjjjjjj.
....jjjjjjjjjjjjjjj.
....jjjjjjjjjjjjjjj.
....jjjjjjjjjjjjjjj.
.....rrrrr..rrrrr...
.....rrrrr..rrrrr...
.....rrrrr..rrrrr...
.....rrrrr..rrrrr...
.....rrrrr..rrrrr...
.....rrrr....rrrr...
.....rrrr....rrrr...
....wwwww...wwwww...
...wwwwww...wwwwww..
""")


LEAN = Sprite("""
........pp.pp.................
.......peepeeppppppp..........
......peeeeeeeeeeeeep.........
.....peeeeHHHeeeeeep..........
.....peepHHHHHHHHHHp..........
......pppHHHHHHHHHHHp.........
........pHHHHHHHHHHHp.........
.......pKKKKKKKKKKKKp.........
.....YYpFQQQQQQWQQQp..........
....YOYpKQQQQQQQQQQp..........
.....pYppHHHHHHHHHp...........
.......pHHHHHHHHHHp...........
........pHHHHHHHHp............
........gMMMMMMMMMg...........
.....gMgMMgvvzzvvMMgMMg.......
....gMMgvvvvvzzvvvvgMMMg......
....MMgvAvvvvzzvvvvvgMMg......
...WWvvAvvYvvzzvvvvvvvgM......
..WWWAvvAvvvvzzvvvvvvvAv......
..WWvAvvAvvvvzzvvvvvvvvv......
...vvAvvvvvvAAzAAvvvvvvvv.....
....vvvvvvvvAAzAAvvvvvvvv.....
.....vvvvvvvAAzAAvvvvvxxv.....
.....vvvvvvvAAzAAvvvvvxxv.....
......vvvvvvAAzAAvvvvvvv......
......vvvvvvAAzAAvvvvvvv......
......vvvvvvAzAAvvvvvvv.......
......vvvvvvAzAAvvvvvvv.......
......vvvvvvvvvvvvvvvvv.......
.......vvvvvvvvvvvvvvv........
.......vvvvvvvvvvvvvvv........
......yzzzz..yzzzz............
.....yzzzz..yzzzz.............
.....yzzzz..yzzzz.............
....yzzzz..yzzzz..............
....yzzzz..yzzzz..............
...yzzzz..yzzzz...............
...yzzzz.yzzzz................
..yzzzz.yzzzz.................
..yzzzzzzzzz..................
..yzzzzzzzz...................
.yzzzz.yzzz...................
.yzzz..yzzz...................
wwwww..wwww...................
wwwwww.wwwww..................
""")

# Eugene: a plain mallard. Green head, white collar, brown chest, grey body. No visor, no coat,
# no interest in the bridge. Faces left.
EUGENE = Sprite("""
....pHHHp...
...pHHHHHp..
.OOOpKHHHp..
....pHHHp...
.....pWWp...
..gBBgggggggx
.gBBgggggggggx
..xxxxxxxxxxx.
""")
# eye half closed: he's not impressed
EUGENE_ALOOF = Sprite(EUGENE.rows[:2] + ["...pHpHHHp..", ".OOOppHHHp.."] + EUGENE.rows[4:])
# bottoms up: head under, tail in the air
EUGENE_DABBLE = Sprite("""
.........gggx
........gggxx
..gBBggggggx.
.gBBgggggggx.
..xxxxxxxxxx.
""")
# asleep: head tucked back into the wing
EUGENE_SLEEP = Sprite("""
.....pHHHp...
....pHHHHHp..
..gBBOOHHHpgx
.gBBgggggggggx
..xxxxxxxxxxx.
""")


EUGENE_ALERT = Sprite("""
....pHHHp...
...pHKHHHp..
.OOOpWHHHp..
....pHHHp...
....pHHHp...
.....pWWp...
..gBBgggggggx
.gBBgggggggggx
..xxxxxxxxxxx.
""")


def eugene(img, d, t, night=False, boat_x=None):
    """Eugene lives on the left of the canal. He drifts, bobs and dabbles, and only ever looks up
    for one thing: a boat. Then he gets a "!" and turns to face it."""
    cycle = t % 48
    ex = 8 + int(5 * math.sin(t / 11.0))
    wl = 76 + (1 if (t // 5) % 2 else 0)           # waterline
    flip = False
    if boat_x is not None and not night:
        sp = EUGENE_ALERT
        flip = boat_x > ex                          # face the boat
        hop = 1 if (t // 2) % 2 else 0
        wl -= hop
    elif night:
        sp = EUGENE_SLEEP
    elif 30 <= cycle < 40:
        sp = EUGENE_DABBLE
    elif 14 <= cycle < 24 or 42 <= cycle:
        sp = EUGENE_ALOOF
    else:
        sp = EUGENE
    stamp(img, sp, ex, wl - sp.h + 1, flip=flip)
    for k in range(ex - 2, ex + sp.w + 2):
        if (k + t) % 3 == 0:
            put(img, k, wl + 1, "i")
    if sp is EUGENE_DABBLE:
        for b in range(3):
            if (t + b * 3) % 6 < 3:
                put(img, ex - 1 - b, wl - 1 - b, "s")
    if sp is EUGENE_SLEEP:                          # little z's drifting up and to the right
        for k in range(2):
            ph = (t + k * 8) % 16
            zx, zy = ex + 8 + ph // 3, wl - sp.h - 1 - ph
            for j, row in enumerate(("QQ", ".Q", "QQ") if ph < 8 else ("mm", ".m", "mm")):
                for i, ch in enumerate(row):
                    if ch != ".":
                        put(img, zx + i, zy + j, ch)
    if sp is EUGENE_ALERT:                          # "!" bubble, bouncing
        bob = 1 if (t // 3) % 2 else 0
        bx0, by0 = ex + 2, wl - sp.h - 11 + bob          # straight above his head
        d.rectangle((bx0, by0, bx0 + 6, by0 + 8), fill=C("K"), outline=C("F"))
        d.rectangle((bx0 + 3, by0 + 2, bx0 + 3, by0 + 5), fill=C("Y"))
        put(img, bx0 + 3, by0 + 7, "Y")
        put(img, bx0 + 2, by0 + 9, "F")


POSTER = Sprite("""
llllllllllllllll
lddddddddddddddl
ldddpHHHHHHpdddl
lddpHHHHHHHHpddl
ldpHHHHHHHHHHpdl
ldpKQQQQQQQQFpdl
ldpHHHHHHHHHHpdl
lddpHHYYYYHHpddl
lddpHHOOOOHHpddl
ldddpHHHHHHpdddl
lddddppppppddddl
ldddddddddddddol
ldooooooooooooll
ldddddddddddddol
llllllllllllllll
""")


SHADE = {"f": ("h", "d"), "d": ("f", "a")}  # base colour -> (lit edge, dark side face)


def building(img, d, x0, x1, top, col, win, seed, t, density=0.5):
    lit, dark = SHADE[col]
    d.rectangle((x0, top, x1, 64), fill=C(col))
    d.rectangle((x1 - 2, top, x1, 64), fill=C(dark))       # side face in shadow
    d.rectangle((x0, top, x0, 64), fill=C(lit))             # lit edge
    d.rectangle((x0, top, x1 - 2, top), fill=C(lit))        # roof edge
    d.rectangle((x1 - 2, top + 1, x1, top + 1), fill=C(col))
    rng = random.Random(seed)
    for y in range(top + 2, 64, 3):
        for x in range(x0 + 2, x1 - 3, 2):
            r = rng.random()
            if r < density and not (rng.random() < 0.05 and (t + x) % 7 == 0):
                put(img, x, y, win[int(r * 1000) % len(win)])


BOAT = Sprite("""
..............i...........
..............iQ..........
..............iQQ.........
........xxxxxxiQQQ........
........xooxooxiQQ........
........xxxxxxxxxx........
.....xxxxxxxxxxxxxxxx.....
.....xkkkkkkkkkkkkkkxx....
xxxxxxkkkkkkkkkkkkkkkxxxx.
xkkkkkkkkkkkkkkkkkkkkkkkxx
.xFFFFFFFFFFFFFFFFFFFFFFx.
..xaaaaaaaaaaaaaaaaaaaax..
....xxxxxxxxxxxxxxxxxx....
""")

ZZ = Sprite("QQQ\n..Q\n.Q.\nQ..\nQQQ")

GX, GY = 72, 57         # where Garrison stands (top-left of the sprite)

# The look of one frame. ``live.py`` fills this from the device state; the bundled GIFs set it by hand.
DEFAULT: dict = {
    "lift": 0.0,            # 0 closed .. 1 fully raised drawbridge leaves
    "boat_x": None,         # x of the boat's bow in art pixels, or None for no boat
    "ring": "Q",            # palette letter for the rim ring (status colour)
    "cam_ok": True,         # camera streaming; False puts a red X on the visor
    "hot": False,           # CPU hot: heat shimmer and an amber visor tick
    "brownout": False,      # under-voltage: a flickering dark band
    "rec": False,           # recording: red dot
    "night": False,         # quiet hours: Garrison and Eugene sleep
    "offline": False,       # backend unreachable: the city goes dark
    "label": "",            # caption strip at the bottom
    "rain": 1.0,            # 0 dry, 1 drizzle, 2 downpour
    "fog": 0.0,             # 0 clear .. 1 thick fog
    "snow": False,
    "storm": False,         # lightning every twelve frames
    "lowvis": False,        # visibility too low for the detector: scanning visor
    "traffic": "flowing",   # flowing | standstill | unknown
    "density": 0.5,         # cars on the bridge while flowing, 0 .. 1
    "wait": "",             # wait-time text shown above the bridge, e.g. "WAIT 4M"
    "label_color": "o",     # palette letter for the caption (offline is always red)
}


def draw(t: int, st: dict | None = None) -> Image.Image:
    """One 120x120 frame of the canal at animation tick ``t`` with the look in ``st`` (see DEFAULT)."""
    st = {**DEFAULT, **(st or {})}
    img = Image.new("RGB", (S, S), C("a"))
    d = ImageDraw.Draw(img)
    # sky: deep teal gradient with dithering
    for y in range(72):
        amt = y / 72
        for x in range(S):
            img.putpixel((x, y), C("d") if kit.BAYER[y % 4][x % 4] < amt * 16 else C("a"))
    if st["rain"] == 0 and not st["snow"] and not st["fog"]:
        for k, (sx, sy) in enumerate(((12, 8), (30, 4), (52, 10), (70, 3), (90, 7), (104, 14), (40, 16), (82, 18))):
            if (t + k * 3) % 10 < 7:
                put(img, sx, sy, "W" if k % 2 else "i")
    # far towers (lighter teal, hazy)
    for x0, x1, top, seed in ((0, 14, 18, 1), (14, 30, 30, 2), (30, 40, 10, 3), (40, 58, 26, 4), (58, 70, 6, 5),
                              (70, 86, 20, 6), (86, 102, 34, 7), (102, 120, 14, 8)):
        building(img, d, x0, x1, top, "f", ["h", "i", "h"], seed, t, 0.05 if st["offline"] else 0.25 * (0.4 if st["night"] else 1))
    # antenna lights
    for x in (34, 62, 108):
        put(img, x, {34: 9, 62: 5, 108: 13}[x], "R" if (t + x) % 10 < 5 else "f")
    # near towers (darker, sharp windows)
    for x0, x1, top, seed, win in ((4, 22, 40, 11, ["Q", "o", "d"]), (24, 36, 48, 12, ["l", "Q", "d"]),
                                   (84, 100, 44, 13, ["o", "Q", "l"]), (100, 120, 38, 14, ["Q", "d", "m"])):
        building(img, d, x0, x1, top, "d", win, seed, t, 0.05 if st["offline"] else 0.45 * (0.4 if st["night"] else 1) * (0.5 if st["brownout"] and t % 3 else 1))
    # neon strips on near towers
    for x in range(44, 52):
        pass
    d.rectangle((100, 40, 101, 60), fill=C("l"))
    d.rectangle((110, 46, 111, 66), fill=C("Q"))
    d.rectangle((8, 42, 9, 56), fill=C("o"))
    # vertical sign on the right tower
    for k, ch in enumerate("HUTCH"):
        text(img, ch, 104, 42 + k * 6, "l")
    # drawbridge across the canal, centre stage: two towers, a split deck with both leaves
    # part-raised, cables to the tips, lamps along the deck. Lit from the left.
    # haze band behind the bridge so it separates from the towers
    for y in range(34, 66):
        amt = (y - 34) / 32
        for x in range(S):
            if kit.BAYER[y % 4][x % 4] < amt * 10:
                put(img, x, y, "w" if st["hot"] and (x + y) % 2 else "f")
    deck_y = 58
    lx, rx = 20, 78
    for tx in (lx, rx):
        d.rectangle((tx, 38, tx + 6, 70), fill=C("h"))
        d.rectangle((tx, 38, tx, 70), fill=C("i"))          # lit edge
        d.rectangle((tx + 5, 38, tx + 6, 70), fill=C("d"))  # shadow face
        d.rectangle((tx - 1, 36, tx + 7, 37), fill=C("i"))  # cap
        d.rectangle((tx - 1, 38, tx + 7, 38), fill=C("a"))  # shadow under the cap
        d.rectangle((tx + 2, 33, tx + 4, 35), fill=C(("F" if (t // 2) % 2 else "Y") if st["lift"] > 0 else "d"))  # warning lamp
        d.rectangle((tx + 1, 42, tx + 4, 46), fill=C("d"))  # operator window
        put(img, tx + 2, 43, "o")
    # fixed approach spans
    for x0, x1 in ((0, lx), (rx + 6, S)):
        d.rectangle((x0, deck_y, x1, deck_y), fill=C("i"))
        d.rectangle((x0, deck_y + 1, x1, deck_y + 3), fill=C("f"))
        d.rectangle((x0, deck_y + 4, x1, deck_y + 4), fill=C("a"))
    # the two leaves, hinged at the towers, raised about 25 degrees
    lift = st["lift"] * 1.1
    span = (rx - lx - 7) / 2
    for pivot, sign in ((lx + 7, 1), (rx - 1, -1)):
        tipx = pivot + sign * span * math.cos(lift)
        tipy = deck_y - span * math.sin(lift)
        # a thick leaf: lit top edge, mid face, dark underside, and railing posts along the top
        d.polygon([(pivot, deck_y), (tipx, tipy), (tipx, tipy + 5), (pivot, deck_y + 5)], fill=C("f"))
        d.line((pivot, deck_y, tipx, tipy), fill=C("i"))
        d.line((pivot, deck_y + 1, tipx, tipy + 1), fill=C("h"))
        d.line((pivot, deck_y + 4, tipx, tipy + 4), fill=C("d"))
        d.line((pivot, deck_y + 5, tipx, tipy + 5), fill=C("a"))
        for k in range(1, 6):
            fx = pivot + sign * span * math.cos(lift) * k / 6
            fy = deck_y - span * math.sin(lift) * k / 6
            d.line((fx, fy - 1, fx, fy - 4), fill=C("x"))
            put(img, int(fx), int(fy) - 5, "Q" if (t + k) % 6 else "W")
        # counterweight below the pivot, swinging back into the tower
        d.rectangle((pivot - sign * 1 - 2, deck_y + 6, pivot - sign * 1 + 1, deck_y + 12), fill=C("d"))
    # traffic on the deck. Flowing: two lanes of cars stream across, the far lane heading left,
    # the near lane right, spacing set by density. Standstill (bridge not closed): cars queue at
    # the gates with brake lights pulsing. Unknown: an empty deck.
    def car(x, y, heading, stopped=False, far=False):
        w = 4 if far else 5
        body = "x" if not far else "k"
        d.rectangle((x, y, x + w - 1, y + 1), fill=C(body))
        d.rectangle((x + 1, y - 1, x + w - 2, y - 1), fill=C("j"))      # roof
        head, tail = (x + w - 1, x) if heading > 0 else (x, x + w - 1)
        put(img, head, y + 1, "T")                                       # headlight
        put(img, tail, y + 1, "R" if not stopped or (t // 3) % 2 else "w")  # tail / brake light
    if st["traffic"] == "flowing" and st["lift"] == 0:
        spacing = int(26 - 16 * st["density"])
        speed = 3 if st["density"] < 0.7 else 1
        for k in range(0, S + 40, spacing):
            x = (k + t * speed) % (S + 30) - 15
            car(x, 55, +1)
        for k in range(0, S + 40, spacing + 5):
            x = S + 10 - ((k + t * speed) % (S + 30))
            car(x, 53, -1, far=True)
    elif st["traffic"] == "standstill":
        for k in range(4):                                                 # queue on each approach
            car(lx - 10 - k * 7, 55, +1, stopped=True)
            car(rx + 12 + k * 7, 53, -1, stopped=True, far=True)
        for gx_ in (lx - 4, rx + 9):                                       # gate arms down
            for yy in range(49, 57):
                put(img, gx_, yy, "Y" if yy % 2 else "K")
    # traffic barrier lights on the approaches
    for x in range(4, lx, 8):
        put(img, x, deck_y - 1, "o" if (t + x) % 8 < 5 else "f")
    for x in range(rx + 8, S, 8):
        put(img, x, deck_y - 1, "o" if (t + x) % 8 < 5 else "f")
    # canal water with reflections
    d.rectangle((0, 64, S, 80), fill=C("a"))
    px = img.load()
    for y in range(65, 80):
        wob = int(2 * math.sin((y + t) / 2.5))
        for x in range(S):
            sy = 64 - (y - 64) * 2
            if 0 <= sy < 72:
                src = px[(x + wob) % S, sy]
                if src not in (C("a"), C("d"), C("f")) and (x + y + t) % 2 == 0:
                    px[x, y] = tuple(v * 3 // 5 for v in src)
    if st["boat_x"] is not None:
        bx = int(st["boat_x"])
        stamp(img, BOAT, bx, 64)
        for k in range(bx - 6, bx):                 # wake
            if (k + t) % 2 == 0:
                put(img, k, 75, "i")
        put(img, bx + 10, 68, "W" if (t // 2) % 2 else "o")   # cabin light
    eugene(img, d, t, night=st["night"] or st["offline"], boat_x=st["boat_x"])
    # walkway: wet asphalt with neon streaks
    d.rectangle((0, 80, S, S), fill=C("r"))
    for y in range(81, S, 3):
        for x in range(0, S, 5):
            if (x // 5 + y) % 3 == 0:
                put(img, x, y, "t")
    # railing
    d.rectangle((0, 78, S, 79), fill=C("x"))
    d.rectangle((0, 80, S, 80), fill=C("t"))
    for x in range(2, S, 8):
        d.rectangle((x, 79, x, 84), fill=C("t"))
        d.rectangle((x - 1, 79, x - 1, 84), fill=C("x"))
    # (the poster board is gone: Eugene needs the sky above him for his "!")
        # street lamp cone on the right
    d.rectangle((24, 74, 25, 104), fill=C("t"))
    d.rectangle((20, 73, 29, 74), fill=C("W"))
    # a patrol car nosing in from the right edge, only the front half in frame. Garrison leans
    # back on the front fender. Lit from the left: light edges on the left, shadow faces right.
    gx, gy = GX, GY
    ground = 104
    nose = 62
    # wheel arch shadow + body
    d.polygon([(nose, 88), (nose + 2, 82), (nose + 26, 80), (nose + 34, 70), (S, 68), (S, 98), (nose, 98)], fill=C("k"))
    d.polygon([(nose + 26, 80), (nose + 34, 70), (S, 68), (S, 80)], fill=C("j"))      # cabin
    d.polygon([(nose + 27, 79), (nose + 34, 71), (nose + 44, 70), (nose + 38, 79)], fill=C("i"))  # windscreen
    d.line((nose + 2, 82, nose + 26, 80), fill=C("x"))                                 # lit hood edge
    d.line((nose + 34, 70, S, 68), fill=C("x"))                                        # roof edge
    d.rectangle((nose, 90, S, 91), fill=C("W"))                                        # white side stripe
    d.rectangle((nose, 95, S, 98), fill=C("z"))                                        # sill shadow
    d.rectangle((nose + 44, 82, nose + 45, 96), fill=C("a"))                           # door line
    for k, ch in enumerate("PD"):                                                      # door lettering
        text(img, ch, nose + 36 + k * 4, 84, "W")
    # light bar: cyan/magenta halves flash
    d.rectangle((nose + 36, 64, S, 67), fill=C("t"))
    d.rectangle((nose + 36, 64, nose + 43, 66), fill=C("Q" if (t // 2) % 2 else "d"))
    d.rectangle((nose + 44, 64, S, 66), fill=C("F" if (t // 2) % 2 == 0 else "d"))
    # headlight and its cone on the wet ground
    d.rectangle((nose, 84, nose + 2, 87), fill=C("M"))
    for cy in range(99, 112):
        for cx in range(nose - 2 - (cy - 99) * 3, nose):
            if 0 <= cx and kit.BAYER[cy % 4][cx % 4] < 5:
                put(img, cx, cy, "t")
    # front wheel
    d.ellipse((nose + 8, 92, nose + 20, ground), fill=C("a"))
    d.ellipse((nose + 11, 95, nose + 17, ground - 3), fill=C("x"))
    put(img, nose + 13, 97, "a")
    for sx in range(gx - 2, gx + 16):        # cast shadow pooling under his feet
        for sy in range(gy + 44, gy + 48):
            if kit.BAYER[sy % 4][sx % 4] < 9:
                put(img, sx, sy, "a")
    stamp(img, LEAN, gx, gy)
    # visor glow spills onto the cheek and the cup
    put(img, gx + 8, gy + 10, "o")
    put(img, gx + 3, gy + 17, "o")
    for k in range(3):  # steam
        sx = gx + 2 + ((t + k * 2) % 4 > 1)
        sy = gy + 15 - k * 2 - (t % 4) // 2
        put(img, sx, sy, "o" if k < 2 else "i")
    # ground reflections: character, poster and sign, flipped and broken up
    for y in range(81, S):
        src_y = 80 - (y - 80)
        for x in range(S):
            if src_y < 30 or (x + y + t) % 3 == 0 or (y // 2 + t // 3) % 4 == 0:
                continue
            if px[x, y] not in (C("r"), C("t")):  # only paint onto bare asphalt
                continue
            src = px[x, src_y]
            if src in (C("r"), C("t"), C("a"), C("x")):
                continue
            px[x, y] = tuple(v * 2 // 5 for v in src)
    # rain: thin pale streaks
    rng = random.Random(5)
    n_rain = 0 if st["night"] or st["offline"] or st["snow"] else int(70 * st["rain"])
    for _ in range(n_rain):
        x0, y0, sp = rng.randrange(S), rng.randrange(S), rng.choice((3, 4, 5)) if st["rain"] <= 1 else rng.choice((6, 7, 8))
        y = (y0 + t * sp) % S
        x = (x0 - (t * sp) // (4 if st["rain"] <= 1 else 2)) % S
        for k in range(3 if st["rain"] <= 1 else 6):
            put(img, x - k // 2, y - k, "i" if k == 0 else "h")
        if st["rain"] > 1 and y > 100 and (x + t) % 3 == 0:   # splashes on the walkway
            put(img, x - 1, y, "i"); put(img, x + 1, y, "i")
    if st["snow"]:
        for _ in range(60):
            x0, y0, sp = rng.randrange(S), rng.randrange(S), rng.choice((1, 1, 2))
            y = (y0 + t * sp) % S
            x = (x0 + int(2 * math.sin((y + x0) / 6))) % S
            put(img, x, y, "W" if sp == 2 else "g")
        # settled snow on the flat edges: railing, car hood and roof, bridge deck
        d.rectangle((0, 77, S, 77), fill=C("W"))
        d.line((nose + 2, 81, nose + 26, 79), fill=C("W"))
        d.line((nose + 34, 69, S, 67), fill=C("W"))
        d.rectangle((0, deck_y - 1, lx, deck_y - 1), fill=C("W"))
        d.rectangle((rx + 6, deck_y - 1, S, deck_y - 1), fill=C("W"))
    if st["storm"] and t % 12 in (0, 1):   # lightning: the sky and the water go pale for two frames
        px = img.load()
        for y in range(80):
            for x in range(S):
                r, g, b = px[x, y]
                px[x, y] = (min(255, r + 90), min(255, g + 100), min(255, b + 110))
        d.line((70 - t, 0, 66, 18, 72, 20, 60, 44), fill=C("W"), width=1)
    if st["fog"] > 0:   # depth fog: heavy on the far towers, lighter on the bridge, light up close
        px = img.load()
        fc = C("h")
        for y in range(S):
            depth = 1.0 if y < 50 else 0.75 if y < 64 else 0.5 if y < 80 else 0.25
            f = min(0.92, st["fog"] * depth)
            for x in range(S):
                r, g, b = px[x, y]
                px[x, y] = (int(r + (fc[0] - r) * f), int(g + (fc[1] - g) * f), int(b + (fc[2] - b) * f))
        # fog bank drifting across the canal
        for x in range(S):
            yy = 70 + int(3 * math.sin((x + t * 2) / 9))
            for k in range(4):
                if kit.BAYER[(yy + k) % 4][x % 4] < 10 - k * 2:
                    put(img, x, yy + k, "i")
    if st["lowvis"]:   # the detector can't see the bridge: the visor shows a scanning bar
        for vx in range(gx + 8, gx + 19):
            for vy in (gy + 8, gy + 9):
                put(img, vx, vy, "Y" if (vx + t * 2) % 11 == 0 else "z")
    # visor: static when the camera is down, dim at night, dead when offline
    if not st["cam_ok"] or st["offline"] or st["night"]:
        for vx in range(gx + 8, gx + 19):
            for vy in (gy + 8, gy + 9):
                if st["offline"] or st["night"]:
                    put(img, vx, vy, "y" if st["offline"] else "m")
                elif (vx * 7 + t * 3 + vy) % 5 < 2:
                    put(img, vx, vy, "R" if (vx + t) % 9 == 0 else "x")
                else:
                    put(img, vx, vy, "z")
    if st["night"]:
        for k in range(2):
            ph = (t + k * 8) % 16
            stamp(img, ZZ, gx + 20 + ph // 4, gy - 2 - ph)
    if st["rec"] and t % 6 < 4:
        d.rectangle((104, 38, 106, 40), fill=C("R"))
    if st["brownout"] and t % 7 == 3:  # power stutter: shift a band sideways
        y0 = 20 + (t * 13) % 70
        band = img.crop((0, y0, S, y0 + 3))
        img.paste(band, (3, y0))
    if st["offline"]:
        px = img.load()
        for y in range(S):
            for x in range(S):
                r, g, b = px[x, y]
                px[x, y] = (int(r * 0.5), int(g * 0.4), int(b * 0.5))
    if st["wait"]:
        tw = len(st["wait"]) * 4 - 1
        d.rectangle((60 - tw // 2 - 3, 12, 60 + tw // 2 + 2, 20), fill=C("a"))
        text(img, st["wait"], 60 - tw // 2, 14, "Y")
    if st["label"]:
        tw = len(st["label"]) * 4 - 1
        d.rectangle((60 - tw // 2 - 3, 103, 60 + tw // 2 + 2, 111), fill=C("a"))
        text(img, st["label"], 60 - tw // 2, 105, "R" if st["offline"] else st["label_color"])
    return img




