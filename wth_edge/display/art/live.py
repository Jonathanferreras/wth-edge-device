"""The canal scene drawn live from the device state, so the art doubles as the status screen.

What each piece of telemetry looks like:

* ring colour        overall status: cyan ok, amber degraded, red offline, violet starting
* bridge             follows ``bridge.position``; lamps flash while it moves or is open
* boat               sails through in ``boat.direction`` (w2e left to right) while one is seen
* mallard            flaps under a "!" while the bridge isn't closed or a boat is passing
* visor              glows while the camera is ok; dark with static when it is down
* sun                yellow when the CPU is cool, orange then pulsing red as it heats up
* skyline            browns out and stutters on under-voltage
* red dot by "WTH"   recording
* ticker             scrolls temperature, input voltage, camera fps and any warnings
* night              the mallard sleeps (unless something is happening) and the city dims

``LiveScene.frame`` is called about ``fps`` times a second by the display service.
"""

from __future__ import annotations

import math
import time
from typing import Any

from PIL import Image, ImageDraw

from ...units import c_to_f
from . import scenes as sc
from .kit import SIZE, c, finish, put, rect, ring, stamp, text, text_width

STATUS_RING = {"ok": "Q", "degraded": "Y", "offline": "R"}
MOVING = ("opening", "closing")
TICKER_Y = 66
TICKER_FRAMES = 12   # each reading stays up for 1.5 s at 8 fps
COOL_C, HOT_C = 50.0, 80.0   # sun goes from yellow to red across this range


def _heat(temp_c: float | None) -> float | None:
    if temp_c is None:
        return None
    return max(0.0, min(1.0, (temp_c - COOL_C) / (HOT_C - COOL_C)))


def _is_night(now: float, night_hours: tuple[int, int]) -> bool:
    start, end = night_hours
    h = time.localtime(now).tm_hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def ticker_items(state: dict[str, Any] | None, status: str, age_s: float | None, temp_unit: str = "F") -> list[tuple[str, str]]:
    """(text, colour) pieces for the scrolling ticker."""
    if status == "offline":
        return [("OFFLINE", "R")] + ([(f"NO UPDATE {int(age_s)}S", "G")] if age_s is not None else [])
    if status == "waiting" or state is None:
        return [("STARTING", "V")]
    items: list[tuple[str, str]] = []
    tel = state.get("telemetry") or {}
    if (t := tel.get("cpu_temp_c")) is not None:
        val = c_to_f(t) if temp_unit.upper() == "F" else t
        items.append((f"{val:.0f}{temp_unit.upper()}", "R" if (_heat(t) or 0) >= 0.75 else "W"))
    if (v := tel.get("input_voltage_v")) is not None:
        items.append((f"{v:.2f}V", "W"))
    cam = state.get("camera") or {}
    items.append((f"{cam.get('fps', 0):.0f}FPS", "E") if cam.get("ok") else ("CAM DOWN", "R"))
    if (tel.get("throttled") or {}).get("under_voltage_now"):
        items.append(("LOW PWR", "Y"))
    boat = state.get("boat") or {}
    if boat.get("present"):
        d = (boat.get("direction") or "").upper()
        items.append((f"BOAT {d[0]}-{d[-1]}" if len(d) == 3 else "BOAT", "Y"))
    pos = str((state.get("bridge") or {}).get("position", "unknown")).lower()
    # Short labels: about 13 characters fit across the bottom of the circle.
    label = {"closed": "BRIDGE DOWN", "open": "BRIDGE OPEN", "opening": "OPENING", "closing": "CLOSING"}
    items.append((label.get(pos, "BRIDGE ?"), "Q" if pos == "closed" else "G" if pos not in label else "Y"))
    if (state.get("recording") or {}).get("recording"):
        items.append(("REC", "R"))
    return items


class LiveScene:
    def __init__(self, temp_unit: str = "F", night_hours: tuple[int, int] = (22, 6)) -> None:
        self.temp_unit = temp_unit
        self.night_hours = night_hours
        self.t = 0
        self.lift = 0.0
        self.boat_x = -12.0
        self.scroll = 0

    def frame(self, state: dict[str, Any] | None, status: str, age_s: float | None = None,
              now: float | None = None) -> Image.Image:
        now = time.time() if now is None else now
        t = self.t
        self.t += 1
        st = state or {}
        live = status not in ("offline", "waiting")
        bridge = st.get("bridge") or {}
        pos = str(bridge.get("position", "unknown")) if live else "unknown"
        boat = (st.get("boat") or {}) if live else {}
        cam_ok = bool((st.get("camera") or {}).get("ok")) and live
        tel = st.get("telemetry") or {}
        heat = _heat(tel.get("cpu_temp_c")) if live else None
        brownout = bool((tel.get("throttled") or {}).get("under_voltage_now")) and live
        rec = bool((st.get("recording") or {}).get("recording")) and live
        busy = pos in ("opening", "open", "closing") or bool(boat.get("present"))
        night = _is_night(now, self.night_hours) and not busy

        # bridge eases towards its real position, slowly while it is moving
        target = 1.0 if pos in ("opening", "open") else 0.0
        step = 0.02 if pos in MOVING else 0.08
        self.lift += max(-step, min(step, target - self.lift))

        on_water = None
        if boat.get("present"):
            east = str(boat.get("direction") or "w2e").lower().startswith("w")
            self.boat_x = self.boat_x + 1.2 if self.boat_x < SIZE + 2 else -12.0
            bx = int(self.boat_x) if east else SIZE - 11 - int(self.boat_x)

            def on_water(im, bx=bx, east=east):
                stamp(im, sc.BOAT, bx, 41, flip=not east)
        else:
            self.boat_x = -12.0

        lights = 0.05 if not live else (0.25 if night else 0.6)
        img = sc.canal_scene(t, lift=self.lift, on_water=on_water, night=night or not live, heat=heat,
                             brownout=brownout, rec=rec, lights_on=lights)

        lamps_flash = busy and (t // 2) % 2 == 0
        if lamps_flash:
            for x in (34, 76):
                rect(img, (x - 1, 35, x + 2, 37), "Y")

        if night:
            bob = 1 if (t // 10) % 2 else 0
            sc._duck(img, sc.MALLARD_SLEEP, t // 3, bob, camera_ok=cam_ok)
            for k in range(3):
                phase = (t + k * 13) % 40
                zx = sc.DUCK_X + 12 + int(4 * math.sin(phase / 5)) + phase // 6
                zy = 52 - phase
                if 6 < zy < 50:
                    stamp(img, sc.Z if phase < 26 else sc.Z.replace("Q", "V"), zx, zy)
        elif busy:
            hop = (0, 2, 3, 2)[t % 4] if t % 8 < 4 else 0
            sc._duck(img, sc.MALLARD, t, -hop, x=sc.DUCK_X + 2, camera_ok=cam_ok)
            if t % 2 == 0:
                stamp(img, sc.WING, sc.DUCK_X + 10, sc.DUCK_WATERLINE - sc.MALLARD.h - 3 - hop)
            bob = 1 if (t // 3) % 2 else 0
            bx0, by0 = sc.DUCK_X + 22, 36 + bob - hop
            rect(img, (bx0, by0, bx0 + 7, by0 + 9), "K")
            ImageDraw.Draw(img).rectangle((bx0, by0, bx0 + 6, by0 + 8), outline=c("F"))
            rect(img, (bx0 + 3, by0 + 2, bx0 + 4, by0 + 5), "Y")
            put(img, bx0 + 3, by0 + 6, "Y")
        else:
            cycle = t % 48
            sprite = sc.MALLARD
            if cycle in (10, 11, 34):
                sprite = sc.MALLARD_BLINK
            elif cycle in (18, 20):
                sprite = sc.MALLARD_QUACK
            elif 38 <= cycle <= 43:
                sprite = sc.MALLARD_DABBLE
            sc._duck(img, sprite, t, 1 if (t // 4) % 2 else 0, camera_ok=cam_ok)

        sc.rain(img, t, n=12 if night else 26, color="V" if night else "Q")
        self._ticker(img, ticker_items(state, status, age_s, self.temp_unit))
        ring_col = STATUS_RING.get(status, "V")
        ring(img, ring_col, 2)
        if busy and (t // 2) % 2 == 0:
            ring(img, "F", 1)
        if brownout and t % 9 == 4:
            img = sc.glitch(img, t, 3)
        return sc.scanlines(finish(img))

    def _ticker(self, img: Image.Image, items: list[tuple[str, str]]) -> None:
        """One reading at a time, centred on a neon strip, sliding up into place."""
        rect(img, (0, TICKER_Y, SIZE, TICKER_Y + 8), "K")
        for x in range(0, SIZE, 2):
            put(img, x, TICKER_Y, "F")
        if not items:
            return
        k, phase = divmod(self.scroll, TICKER_FRAMES)
        self.scroll += 1
        s, col = items[k % len(items)]
        rise = max(0, 3 - phase)  # slide up over the first few frames
        text(img, s, (SIZE - text_width(s)) // 2, TICKER_Y + 2 + rise, col)


# ---------------------------------------------------------------- demo states, for previews
def _demo(**over) -> dict[str, Any]:
    st = {
        "status": "ok",
        "camera": {"ok": True, "fps": 14.9},
        "telemetry": {"cpu_temp_c": 52.0, "input_voltage_v": 5.08, "throttled": {"under_voltage_now": False}},
        "bridge": {"position": "closed"},
        "boat": {"present": False, "direction": None},
        "recording": {"recording": False},
    }
    for k, v in over.items():
        st[k] = {**st[k], **v} if isinstance(st.get(k), dict) else v
    return st


DEMOS: dict[str, tuple[dict[str, Any] | None, str, float | None, int]] = {
    # name: (state, overall status, state age, local hour)
    "live-ok": (_demo(), "ok", 1.0, 14),
    "live-boat": (_demo(bridge={"position": "open"}, boat={"present": True, "direction": "w2e"},
                        recording={"recording": True}), "ok", 1.0, 14),
    "live-hot-lowpower": (_demo(status="degraded", telemetry={"cpu_temp_c": 82.0, "input_voltage_v": 4.71,
                                                              "throttled": {"under_voltage_now": True}}),
                          "degraded", 1.0, 14),
    "live-camera-down": (_demo(status="degraded", camera={"ok": False, "fps": 0}), "degraded", 1.0, 14),
    "live-night": (_demo(), "ok", 1.0, 23),
    "live-offline": (_demo(), "offline", 95.0, 14),
}


def build_demos(out_dir, frames: int = 64, fps: float = 8.0) -> list:
    """Render each demo state as a GIF so the live look can be checked without a Pi."""
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (state, status, age, hour) in DEMOS.items():
        now = time.mktime((2026, 11, 4, hour, 0, 0, 0, 0, -1))
        scene = LiveScene()
        if state and state["bridge"]["position"] == "open":
            scene.lift = 1.0
        imgs = [scene.frame(state, status, age, now) for _ in range(frames)]
        path = out_dir / f"{name}.gif"
        sc.save_gif(imgs, [int(1000 / fps)] * frames, path, loop=True)
        written.append(path)
    return written
