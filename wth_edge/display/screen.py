"""Draws the round 240x240 status screen from the shared state.

Layout, top to bottom: title, clock, bridge position (large), camera status,
temperature and input voltage, then the address of the local web panel.
The outer ring's colour is the overall status: green ok, amber degraded,
red offline, copper while starting.
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..units import format_temp

W = H = 240
BG = (10, 20, 40)
COPPER = (200, 120, 50)
WHITE = (240, 240, 240)
GREY = (150, 160, 175)
# Same hues the web app uses for traffic (TRAFFIC_CONFIG).
GREEN = (33, 209, 159)
AMBER = (250, 204, 21)
RED = (239, 68, 68)

FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental")]


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf"] if bold else ["DejaVuSans.ttf", "Arial.ttf"]
    for d in FONT_DIRS:
        for n in names:
            p = d / n
            if p.is_file():
                return ImageFont.truetype(str(p), size)
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _centered(d: ImageDraw.ImageDraw, y: int, text: str, size: int, fill, bold=False, max_w: int = 200) -> None:
    while True:
        f = font(size, bold)
        left, top, right, bottom = d.textbbox((0, 0), text, font=f)
        if right - left <= max_w or size <= 9:
            break
        size -= 1
    d.text(((W - (right - left)) // 2 - left, y - (bottom - top) // 2 - top), text, fill=fill, font=f)


def overall(state: dict[str, Any] | None, age_s: float | None, stale_after_s: float) -> str:
    if state is None:
        return "waiting"
    if age_s is None or age_s > stale_after_s:
        return "offline"
    return state.get("status", "starting")


def render(
    state: dict[str, Any] | None,
    age_s: float | None,
    stale_after_s: float = 15.0,
    now: float | None = None,
    temp_unit: str = "F",
) -> Image.Image:
    now = time.time() if now is None else now
    status = overall(state, age_s, stale_after_s)
    ring = {"ok": GREEN, "degraded": AMBER, "offline": RED}.get(status, COPPER)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.ellipse((3, 3, W - 4, H - 4), outline=ring, width=7)

    _centered(d, 40, "WATCH THE HUTCH", 13, COPPER, bold=True, max_w=150)
    _centered(d, 60, time.strftime("%H:%M", time.localtime(now)), 15, GREY)

    if status in ("waiting", "offline"):
        _centered(d, 112, "OFFLINE" if status == "offline" else "WAITING", 30, RED if status == "offline" else WHITE, bold=True)
        detail = "device app not running" if status == "offline" else "starting device app"
        if status == "offline" and age_s is not None:
            detail = f"no update for {int(age_s)} s"
        _centered(d, 146, detail, 13, GREY, max_w=180)
        return img

    bridge = state.get("bridge", {}) or {}
    position = str(bridge.get("position", "unknown")).upper()
    _centered(d, 104, position, 32, WHITE, bold=True, max_w=190)
    conf = bridge.get("confidence")
    sub = "no detector yet" if position == "UNKNOWN" else f"bridge · {round((conf or 0) * 100)}%"
    _centered(d, 134, sub, 12, GREY)

    cam = state.get("camera", {}) or {}
    if cam.get("ok"):
        _centered(d, 160, f"CAM {cam.get('fps', 0):.1f} fps", 14, GREEN, bold=True)
    else:
        _centered(d, 160, "CAMERA DOWN", 14, RED, bold=True)

    tel = state.get("telemetry", {}) or {}
    parts = []
    if (temp := format_temp(tel.get("cpu_temp_c"), temp_unit)) is not None:
        parts.append(temp)
    if tel.get("input_voltage_v") is not None:
        parts.append(f"{tel['input_voltage_v']:.2f} V")
    thr = tel.get("throttled") or {}
    if thr.get("under_voltage_now"):
        parts.append("LOW PWR")
    if parts:
        _centered(d, 180, "  ".join(parts), 13, AMBER if thr.get("under_voltage_now") else WHITE)

    if url := state.get("web_url"):
        _centered(d, 200, url.removeprefix("http://"), 11, GREY, max_w=150)
    return img
