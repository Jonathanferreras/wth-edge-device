"""The bundled animations: the lofi canal from ``lofi.py`` in four moods.

* ``boot``   the city comes out of the fog while the ring draws in, the title types out.
* ``idle``   drizzle, traffic flowing over the bridge, Eugene dabbling, Garrison on watch.
* ``alert``  the bridge lifts, traffic stops at the gates, a boat goes through, Eugene shouts.
* ``sleep``  a dry, quiet night: both ducks asleep, most windows dark.

Each scene is a function returning ``(frames, durations_ms, loop)``. Frames are 120x120 art
canvases; ``build`` masks them to the circle, adds CRT scanlines and scales them 2x to
240x240 GIFs. ``save_gif`` quantises every frame against one palette built from all of them,
so colours never shimmer between frames.

To add a scene: write a function here, add it to ``SCENES``, run ``wth-edge art build``.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from . import lofi
from .kit import LOFI_SIZE, Sprite, finish, ring, stamp

GIF_DIR = Path(__file__).parent / "gifs"
SCANLINE_DIM = 0.6
SCANLINE_EVERY = 4      # darken one screen row in four: a CRT look without eating the 2x pixels
TITLE = "WATCH THE HUTCH"

GLINT = Sprite("W.W\n.W.\nW.W")


def rim(img: Image.Image, color: str = "Q", frac: float = 1.0) -> None:
    """The scene's thin status ring, drawn on the 120 px canvas just inside the circle."""
    ring(img, "a", 1, frac)
    px = img.load()
    size = img.width
    for y in range(size):
        for x in range(size):
            if (x + y) % 2 == 0:
                continue
            dx, dy = x + 0.5 - size / 2, y + 0.5 - size / 2
            d = (dx * dx + dy * dy) ** 0.5
            if size / 2 - 1.5 <= d <= size / 2 and px[x, y] == lofi.C("a"):
                px[x, y] = lofi.C(color)


def boot():
    """Ring draws in while the city comes out of the fog, the title types out, a visor glint."""
    frames, durs = [], []
    n_fog = 20
    for t in range(n_fog):                      # fog lifts, ring draws
        img = lofi.draw(t, {"fog": 1.0 - t / n_fog, "rain": 0.0, "traffic": "unknown"})
        rim(img, "Q", frac=(t + 1) / n_fog)
        frames.append(img)
        durs.append(90)
    for k in range(1, len(TITLE) + 1):         # title types out
        t = n_fog + k
        img = lofi.draw(t, {"rain": 0.0, "traffic": "unknown", "label": TITLE[:k], "label_color": "Q"})
        rim(img)
        frames.append(img)
        durs.append(70 if TITLE[k - 1] != " " else 40)
    for t in range(n_fog + len(TITLE) + 1, n_fog + len(TITLE) + 13):   # hold, glint
        img = lofi.draw(t, {"rain": 0.0, "label": TITLE, "label_color": "Q"})
        rim(img)
        if t % 12 in (3, 4):
            stamp(img, GLINT, lofi.GX + 9, lofi.GY + 7)
        frames.append(img)
        durs.append(120)
    return frames, durs, False


def idle():
    """Drizzle, traffic flowing over the bridge, the windows flicker, Eugene dabbles."""
    frames = []
    for t in range(48):
        img = lofi.draw(t, {"density": 0.45})
        rim(img)
        frames.append(img)
    return frames, [125] * len(frames), True


def alert():
    """The bridge lifts, traffic stops at the gates, a boat goes through, Eugene shouts."""
    frames, durs = [], []
    n = 72
    for t in range(n):
        lift = min(1.0, t / 14) if t < 56 else max(0.0, 1.0 - (t - 56) / 14)
        boat_x = -26 + (t - 12) * 2.4 if 12 <= t < 56 else None
        img = lofi.draw(t, {"lift": lift, "boat_x": boat_x, "traffic": "standstill", "rain": 1.0,
                            "wait": "WAIT 4M" if 8 < t < 60 else ""})
        rim(img, "F" if (t // 2) % 2 else "Y")
        frames.append(img)
        durs.append(125)
    return frames, durs, True


def sleep():
    """A dry, quiet night: both ducks asleep, most windows dark, Zzz over the canal."""
    frames = []
    for t in range(48):
        img = lofi.draw(t, {"night": True, "rain": 0.0, "traffic": "unknown"})
        rim(img, "m")
        frames.append(img)
    return frames, [160] * len(frames), True


SCENES = {"boot": boot, "idle": idle, "alert": alert, "sleep": sleep}


def build(out_dir: Path = GIF_DIR, names: list[str] | None = None) -> list[Path]:
    """Render scenes to 240x240 GIFs (GIFs are both the preview and the asset)."""
    out_dir = Path(out_dir)
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
    """Write frames as one GIF, quantised against a single palette built from all of them."""
    # A strip of every frame (shrunk, so the palette build stays quick) gives one shared palette.
    step = max(1, len(frames) // 16)
    sample = frames[::step]
    strip = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for k, f in enumerate(sample):
        strip.paste(f, (0, k * f.height))
    pal = strip.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    kw = {"loop": 0} if loop else {}
    q[0].save(path, save_all=True, append_images=q[1:], duration=durs, disposal=1, optimize=False, **kw)


def scanlines(img: Image.Image, every: int = SCANLINE_EVERY) -> Image.Image:
    """Darken one screen row in ``every``, like a CRT. Works on the 240x240 image."""
    out = img.copy()
    px = out.load()
    for y in range(every - 1, out.height, every):
        for x in range(out.width):
            r, g, b = px[x, y]
            px[x, y] = (int(r * SCANLINE_DIM), int(g * SCANLINE_DIM), int(b * SCANLINE_DIM))
    return out


__all__ = ["GIF_DIR", "LOFI_SIZE", "SCENES", "build", "rim", "save_gif", "scanlines"]
