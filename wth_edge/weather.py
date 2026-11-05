"""Local weather for the bridge, from Open-Meteo (free, no key), polled on a slow interval.

The sample goes into ``state["weather"]`` so the screen can draw rain, fog, snow and
lightning, and so low visibility can be read off the state when the camera's detections
are in doubt. ``WeatherSource`` is a thread that fills ``snapshot()``; ``classify`` maps the
WMO weather code to the handful of kinds the art understands and is pure, so it tests
without the network.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("wth.weather")

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
FIELDS = "weather_code,precipitation,visibility,temperature_2m,wind_speed_10m,is_day"

# WMO weather interpretation codes -> the kinds the scene draws.
_KINDS = [
    ((95, 96, 99), "thunderstorm"),
    ((71, 73, 75, 77, 85, 86), "snow"),
    ((65, 67, 82), "heavy_rain"),
    ((51, 53, 55, 56, 57, 61, 63, 66, 80, 81), "rain"),
    ((45, 48), "fog"),
    ((1, 2, 3), "cloudy"),
    ((0,), "clear"),
]


def classify(weather_code: int | None) -> str:
    for codes, kind in _KINDS:
        if weather_code in codes:
            return kind
    return "unknown"


def parse(body: dict[str, Any], lowvis_below_m: float, at: float | None = None) -> dict[str, Any]:
    """Turn an Open-Meteo ``current`` block into the state's weather sample."""
    cur = body.get("current") or {}
    code = cur.get("weather_code")
    vis = cur.get("visibility")
    sample = {
        "ok": True,
        "kind": classify(code),
        "weather_code": code,
        "precipitation_mm": cur.get("precipitation"),
        "visibility_m": vis,
        "temperature_c": cur.get("temperature_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "is_day": bool(cur.get("is_day", 1)),
        "lowvis_below_m": lowvis_below_m,
        "low_visibility": vis is not None and float(vis) < lowvis_below_m,
        "at": at if at is not None else time.time(),
        "source": "open-meteo",
    }
    return sample


def fetch(lat: float, lon: float, lowvis_below_m: float, timeout_s: float = 10.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "current": FIELDS,
                                    "timezone": "auto"})
    req = urllib.request.Request(f"{OPEN_METEO}?{query}", headers={"User-Agent": "wth-edge"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return parse(json.loads(r.read()), lowvis_below_m)


class WeatherSource:
    """Polls Open-Meteo every ``interval_s`` and keeps the last good sample."""

    def __init__(self, cfg, lat: float, lon: float) -> None:
        self.cfg = cfg
        self.lat, self.lon = lat, lon
        self._sample: dict[str, Any] = {"ok": False, "error": "no sample yet", "source": "open-meteo"}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="weather", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._sample)

    def poll_once(self) -> dict[str, Any]:
        try:
            sample = fetch(self.lat, self.lon, self.cfg.lowvis_below_m, self.cfg.timeout_s)
        except Exception as exc:  # network down, bad JSON: keep the old sample, say why
            with self._lock:
                old = self._sample
                sample = {**old, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log.warning("weather: %s", sample["error"])
        with self._lock:
            self._sample = sample
        return sample

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.cfg.interval_s)
