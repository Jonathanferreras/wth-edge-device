"""The lofi canal drawn live from the device state, so the art doubles as the status screen.

What each piece of telemetry looks like:

* ring colour        overall status: cyan ok, amber degraded, red offline, lavender starting
* drawbridge         eases towards ``bridge.position``; the warning lamp flashes while it is up
* wait time          the opening state machine's remaining minutes, printed above the bridge
* traffic            cars flow over the bridge at the detector's density; they queue at the
                     gates while the bridge is not closed; an empty bridge means unknown
* boat               crosses the canal in ``boat.direction``; Eugene shouts "!" while it does
* Garrison's visor   cyan while the camera is ok; red X when it is down; amber tick when hot;
                     a scanning bar when the weather hides the bridge
* weather            rain, downpour, fog, snow and lightning follow ``state["weather"]``
* city               browns out and stutters on under-voltage; goes dark when offline
* red dot            recording
* caption            one reading at a time: temperature, voltage, camera fps, warnings
* night              both ducks sleep (unless something is happening) and the city dims

``LiveScene.frame`` is called about ``fps`` times a second by the display service.
"""

from __future__ import annotations

import time
from typing import Any

from PIL import Image

from ...units import c_to_f
from . import lofi
from . import scenes as sc
from .kit import LOFI_SIZE, finish

STATUS_RING = {"ok": "Q", "degraded": "Y", "offline": "R"}
MOVING = ("opening", "closing")
TICKER_FRAMES = 12   # each reading stays up for 1.5 s at 8 fps
COOL_C, HOT_C = 50.0, 80.0
DENSITY = {"light": 0.25, "moderate": 0.55, "heavy": 0.9}
BOAT_W = 26


def _heat(temp_c: float | None) -> float | None:
    if temp_c is None:
        return None
    return max(0.0, min(1.0, (temp_c - COOL_C) / (HOT_C - COOL_C)))


def _is_night(now: float, night_hours: tuple[int, int]) -> bool:
    start, end = night_hours
    h = time.localtime(now).tm_hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def ticker_items(state: dict[str, Any] | None, status: str, age_s: float | None, temp_unit: str = "F") -> list[tuple[str, str]]:
    """(text, colour) readings for the caption strip, shown one at a time."""
    if status == "offline":
        return [("OFFLINE", "R")] + ([(f"NO UPDATE {int(age_s)}S", "x")] if age_s is not None else [])
    if status == "waiting" or state is None:
        return [("STARTING", "m")]
    items: list[tuple[str, str]] = []
    tel = state.get("telemetry") or {}
    if (t := tel.get("cpu_temp_c")) is not None:
        val = c_to_f(t) if temp_unit.upper() == "F" else t
        items.append((f"{val:.0f}{temp_unit.upper()}", "R" if (_heat(t) or 0) >= 0.75 else "o"))
    if (v := tel.get("input_voltage_v")) is not None:
        items.append((f"{v:.2f}V", "o"))
    cam = state.get("camera") or {}
    items.append((f"{cam.get('fps', 0):.0f}FPS", "o") if cam.get("ok") else ("CAM DOWN", "R"))
    if (tel.get("throttled") or {}).get("under_voltage_now"):
        items.append(("LOW PWR", "Y"))
    boat = state.get("boat") or {}
    if boat.get("present"):
        d = (boat.get("direction") or "").upper()
        items.append((f"BOAT {d[0]}-{d[-1]}" if len(d) == 3 else "BOAT", "Y"))
    pos = str((state.get("bridge") or {}).get("position", "unknown")).lower()
    label = {"closed": "BRIDGE DOWN", "open": "BRIDGE OPEN", "opening": "OPENING", "closing": "CLOSING"}
    items.append((label.get(pos, "BRIDGE ?"), "Q" if pos == "closed" else "x" if pos not in label else "Y"))
    if (w := weather_text(state.get("weather"))) is not None:
        items.append(w)
    if (state.get("recording") or {}).get("recording"):
        items.append(("REC", "R"))
    return items


def weather_text(weather: dict[str, Any] | None) -> tuple[str, str] | None:
    """One short weather reading, or None when there is nothing worth saying."""
    if not weather or not weather.get("ok", True):
        return None
    look = weather_look(weather)
    if look["lowvis"]:
        return (f"VIS {int(weather.get('visibility_m') or 0)}M", "Y")
    if look["storm"]:
        return ("STORM", "Y")
    if look["snow"]:
        return ("SNOW", "i")
    if look["rain"] >= 2:
        return ("HEAVY RAIN", "i")
    if look["fog"] >= 0.5:
        return ("FOG", "i")
    return None


def weather_look(weather: dict[str, Any] | None) -> dict[str, Any]:
    """Map the weather sample in ``state["weather"]`` to the scene's rain, fog, snow, storm, lowvis."""
    look: dict[str, Any] = {"rain": 1.0, "fog": 0.0, "snow": False, "storm": False, "lowvis": False}
    if not weather or not weather.get("ok", True):
        return look
    kind = str(weather.get("kind") or "")
    mm = float(weather.get("precipitation_mm") or 0.0)
    look["rain"] = 0.0 if kind in ("clear", "cloudy", "fog") and mm < 0.1 else (2.0 if kind == "heavy_rain" or mm >= 4 else 1.0)
    look["snow"] = kind == "snow"
    look["storm"] = kind == "thunderstorm"
    vis = weather.get("visibility_m")
    if vis is not None:
        vis = float(vis)
        look["fog"] = max(0.0, min(1.0, (4000.0 - vis) / 3500.0))   # 4 km clear .. 500 m thick
        look["lowvis"] = vis < float(weather.get("lowvis_below_m") or 1000.0)
    elif kind == "fog":
        look["fog"] = 0.8
    return look


class LiveScene:
    def __init__(self, temp_unit: str = "F", night_hours: tuple[int, int] = (22, 6)) -> None:
        self.temp_unit = temp_unit
        self.night_hours = night_hours
        self.t = 0
        self.lift = 0.0
        self.boat_x = -float(BOAT_W)
        self.scroll = 0

    def look(self, state: dict[str, Any] | None, status: str, now: float) -> dict[str, Any]:
        """Turn the device state into the scene's look (``lofi.DEFAULT`` keys)."""
        st = state or {}
        live = status not in ("offline", "waiting")
        bridge = st.get("bridge") or {}
        pos = str(bridge.get("position", "unknown")).lower() if live else "unknown"
        opening = (st.get("opening") or {}) if live else {}
        if opening.get("position"):
            pos = str(opening["position"]).lower()
        boat = (st.get("boat") or {}) if live else {}
        cam_ok = bool((st.get("camera") or {}).get("ok")) and live
        tel = st.get("telemetry") or {}
        heat = _heat(tel.get("cpu_temp_c")) if live else None
        brownout = bool((tel.get("throttled") or {}).get("under_voltage_now")) and live
        rec = bool((st.get("recording") or {}).get("recording")) and live
        busy = pos in ("opening", "open", "closing") or bool(boat.get("present"))
        night = _is_night(now, self.night_hours) and not busy and live

        # the bridge eases towards its real position, slowly while it is moving
        target = 1.0 if pos in ("opening", "open") else 0.0
        step = 0.02 if pos in MOVING else 0.08
        self.lift += max(-step, min(step, target - self.lift))
        if abs(self.lift - target) < 1e-9:
            self.lift = target

        boat_x = None
        if boat.get("present"):
            east = str(boat.get("direction") or "w2e").lower().startswith("w")
            self.boat_x = self.boat_x + 1.6 if self.boat_x < LOFI_SIZE + BOAT_W else -float(BOAT_W)
            boat_x = self.boat_x if east else LOFI_SIZE - self.boat_x
        else:
            self.boat_x = -float(BOAT_W)

        # traffic: the web app's rule, standstill unless the bridge is closed
        traffic, density = "unknown", 0.5
        levels = [str((lane or {}).get("level", "unknown")) for lane in (st.get("traffic") or {}).values()] if live else []
        known = [lv for lv in levels if lv in DENSITY]
        if pos != "closed" and pos != "unknown":
            traffic = "standstill" if known else "unknown"
        elif known:
            traffic, density = "flowing", max(DENSITY[lv] for lv in known)
        elif pos == "closed":
            traffic, density = "flowing", 0.3   # bridge down and no traffic detector: light flow

        wait = ""
        if pos in ("opening", "open", "closing"):
            w = opening.get("wait") or {}
            mins = w.get("remainingMinutes")
            if mins is not None:
                wait = f"WAIT {max(1, round(float(mins)))}M"

        look = {
            "lift": self.lift, "boat_x": boat_x, "ring": STATUS_RING.get(status, "m"),
            "cam_ok": cam_ok, "hot": (heat or 0) >= 0.75, "brownout": brownout, "rec": rec,
            "night": night, "offline": not live and status == "offline",
            "traffic": traffic, "density": density, "wait": wait,
        }
        look.update(weather_look(st.get("weather")) if live else {"rain": 0.0})
        if night:
            look["rain"] = min(look["rain"], 1.0)
        return look

    def frame(self, state: dict[str, Any] | None, status: str, age_s: float | None = None,
              now: float | None = None) -> Image.Image:
        now = time.time() if now is None else now
        t = self.t
        self.t += 1
        look = self.look(state, status, now)
        items = ticker_items(state, status, age_s, self.temp_unit)
        if items:
            k, _phase = divmod(self.scroll, TICKER_FRAMES)
            self.scroll += 1
            look["label"], look["label_color"] = items[k % len(items)]
        img = lofi.draw(t, look)
        busy = look["lift"] > 0 or look["boat_x"] is not None
        sc.rim(img, ("F" if (t // 2) % 2 else look["ring"]) if busy else look["ring"])
        return sc.scanlines(finish(img))


# ---------------------------------------------------------------- demo states, for previews
def _demo(**over) -> dict[str, Any]:
    st: dict[str, Any] = {
        "status": "ok",
        "camera": {"ok": True, "fps": 14.9},
        "telemetry": {"cpu_temp_c": 52.0, "input_voltage_v": 5.08, "throttled": {"under_voltage_now": False}},
        "bridge": {"position": "closed"},
        "traffic": {"northbound": {"level": "moderate"}, "southbound": {"level": "light"}},
        "boat": {"present": False, "direction": None},
        "recording": {"recording": False},
        "weather": {"ok": True, "kind": "rain", "precipitation_mm": 0.6, "visibility_m": 9000},
    }
    for k, v in over.items():
        st[k] = {**st[k], **v} if isinstance(st.get(k), dict) and isinstance(v, dict) else v
    return st


DEMOS: dict[str, tuple[dict[str, Any] | None, str, float | None, int]] = {
    # name: (state, overall status, state age, local hour)
    "live-ok": (_demo(), "ok", 1.0, 14),
    "live-boat": (_demo(bridge={"position": "open"},
                        opening={"position": "open", "wait": {"remainingMinutes": 3.6}},
                        boat={"present": True, "direction": "w2e"},
                        recording={"recording": True}), "ok", 1.0, 14),
    "live-heavy-traffic": (_demo(traffic={"northbound": {"level": "heavy"}, "southbound": {"level": "heavy"}}), "ok", 1.0, 17),
    "live-hot-lowpower": (_demo(status="degraded", telemetry={"cpu_temp_c": 82.0, "input_voltage_v": 4.71,
                                                              "throttled": {"under_voltage_now": True}}),
                          "degraded", 1.0, 14),
    "live-camera-down": (_demo(status="degraded", camera={"ok": False, "fps": 0}), "degraded", 1.0, 14),
    "live-fog": (_demo(weather={"kind": "fog", "precipitation_mm": 0.0, "visibility_m": 600}), "ok", 1.0, 9),
    "live-downpour": (_demo(weather={"kind": "heavy_rain", "precipitation_mm": 7.0, "visibility_m": 3000}), "ok", 1.0, 14),
    "live-snow": (_demo(weather={"kind": "snow", "precipitation_mm": 1.0, "visibility_m": 5000}), "ok", 1.0, 14),
    "live-storm": (_demo(weather={"kind": "thunderstorm", "precipitation_mm": 5.0, "visibility_m": 2500}), "ok", 1.0, 14),
    "live-night": (_demo(weather={"kind": "clear", "precipitation_mm": 0.0, "visibility_m": 20000}), "ok", 1.0, 23),
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
