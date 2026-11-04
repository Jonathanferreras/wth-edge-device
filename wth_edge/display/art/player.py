"""Play a pixel art animation on the display driver.

``load`` takes a bundled name (``boot``, ``idle``, ``alert``, ``sleep``), a GIF/PNG file, or a
folder of numbered PNG frames. Frames are padded or scaled to 240x240 with nearest neighbour,
so an 80x80 or 120x120 GIF drawn in Aseprite works as is. ``play`` keeps each frame on screen
for its GIF duration, measured against a monotonic clock so slow SPI writes don't add drift.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageSequence

from .kit import PANEL
from .scenes import GIF_DIR

DEFAULT_MS = 100


@dataclass
class Animation:
    name: str
    frames: list[Image.Image]
    durations_ms: list[int]
    loop: bool

    @property
    def length_s(self) -> float:
        return sum(self.durations_ms) / 1000


def available() -> list[str]:
    return sorted(p.stem for p in GIF_DIR.glob("*.gif"))


def _fit(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    if img.size == (PANEL, PANEL):
        return img
    w, h = img.size
    k = max(1, min(PANEL // w, PANEL // h))
    if w * k > PANEL or h * k > PANEL:  # bigger than the panel: shrink to fit
        k = 1
        img.thumbnail((PANEL, PANEL), Image.NEAREST)
        w, h = img.size
    img = img.resize((w * k, h * k), Image.NEAREST)
    out = Image.new("RGB", (PANEL, PANEL), (0, 0, 0))
    out.paste(img, ((PANEL - img.width) // 2, (PANEL - img.height) // 2))
    return out


def load(name_or_path: str | Path) -> Animation:
    p = Path(name_or_path).expanduser()
    if not p.exists() and (GIF_DIR / f"{name_or_path}.gif").is_file():
        p = GIF_DIR / f"{name_or_path}.gif"
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.suffix.lower() in (".png", ".gif"))
        if not files:
            raise FileNotFoundError(f"no .png frames in {p}")
        frames = [_fit(Image.open(f)) for f in files]
        return Animation(p.name, frames, [DEFAULT_MS] * len(frames), True)
    if not p.is_file():
        raise FileNotFoundError(f"no animation {name_or_path!r}; bundled: {', '.join(available())}")
    im = Image.open(p)
    frames, durs = [], []
    for fr in ImageSequence.Iterator(im):
        frames.append(_fit(fr))
        durs.append(int(fr.info.get("duration") or im.info.get("duration") or DEFAULT_MS))
    # GIFs without a loop extension play once; single images just show.
    loop = "loop" in im.info and len(frames) > 1
    return Animation(p.stem, frames, durs, loop)


def play(dev, anim: Animation, loops: int | None = None, seconds: float | None = None,
         stop: threading.Event | None = None, speed: float = 1.0) -> int:
    """Show ``anim`` on ``dev``; returns frames shown.

    ``loops`` None means the GIF's own setting (forever for looping GIFs, once otherwise).
    ``seconds`` caps the total time. ``stop`` ends playback early.
    """
    stop = stop or threading.Event()
    if loops is None:
        loops = 0 if anim.loop else 1
    end = time.monotonic() + seconds if seconds else None
    shown = 0
    n = 0
    next_t = time.monotonic()
    while not stop.is_set() and (loops == 0 or n < loops):
        for img, ms in zip(anim.frames, anim.durations_ms):
            if stop.is_set() or (end and time.monotonic() >= end):
                return shown
            dev.show(img)
            shown += 1
            next_t += ms / 1000 / speed
            delay = next_t - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            else:
                next_t = time.monotonic()  # fell behind (slow SPI): don't try to catch up
        n += 1
    return shown
