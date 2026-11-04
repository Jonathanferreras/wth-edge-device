"""Vessel positions from AIS, so a boat is known before the camera can see it.

``AisSource`` keeps the vessels within ``radius_m`` of the bridge and works out, for each,
which side of the bridge it is on (east or west, by longitude), whether it is closing on
the bridge, and an ETA. The state machine reads the nearest approaching vessel.

Drivers:

* ``aisstream``: the free aisstream.io websocket (volunteer receivers worldwide). Needs an
  API key in ``WTH_AISSTREAM_KEY`` and the ``websockets`` package (``pip install -e .[ais]``,
  or ``apt install python3-websockets``).

A local receiver (RTL-SDR dongle + AIS-catcher) can feed ``AisSource.report`` with the same
fields later; nothing downstream knows the difference.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

EARTH_M = 6_371_000.0
KNOT_MS = 0.514444


def distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Metres and initial bearing (degrees, 0 = north) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    dlat = p2 - p1
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    d = 2 * EARTH_M * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return d, (math.degrees(math.atan2(y, x)) + 360) % 360


def describe(vessel: dict[str, Any], bridge_lat: float, bridge_lon: float, min_knots: float) -> dict[str, Any]:
    """Add distance, side, closing speed, approaching flag and ETA to a vessel report."""
    v = dict(vessel)
    d, bearing_to_bridge = distance_bearing(v["lat"], v["lon"], bridge_lat, bridge_lon)
    v["distance_m"] = round(d)
    v["side"] = "east" if v["lon"] > bridge_lon else "west"
    sog = v.get("sog_knots")
    cog = v.get("cog_deg")
    closing = None
    if sog is not None and cog is not None:
        # Speed along the line to the bridge, in knots. Positive = getting closer.
        closing = round(sog * math.cos(math.radians(cog - bearing_to_bridge)), 2)
    v["closing_knots"] = closing
    v["approaching"] = closing is not None and closing >= min_knots
    v["eta_s"] = round(d / (closing * KNOT_MS)) if v["approaching"] else None
    return v


class AisSource:
    """Holds the vessel table. A driver thread fills it; readers call ``snapshot``."""

    def __init__(self, cfg, bridge_lat: float, bridge_lon: float) -> None:
        self.cfg = cfg
        self.lat, self.lon = bridge_lat, bridge_lon
        self._vessels: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ok: bool | None = None
        self.error: str | None = None
        self.last_message_at: float | None = None
        self.messages = 0

    @property
    def configured(self) -> bool:
        return bool(self.lat or self.lon)

    # ---- reports in ----
    def report(self, mmsi: int, lat: float, lon: float, sog_knots: float | None = None, cog_deg: float | None = None,
               heading_deg: float | None = None, name: str | None = None, ship_type: int | None = None,
               at: float | None = None) -> dict[str, Any] | None:
        """One position report. Returns the described vessel, or None when out of range."""
        at = time.time() if at is None else at
        self.last_message_at, self.messages = at, self.messages + 1
        v = {"mmsi": mmsi, "lat": lat, "lon": lon, "sog_knots": sog_knots, "cog_deg": cog_deg,
             "heading_deg": heading_deg, "name": name, "ship_type": ship_type, "at": at}
        with self._lock:
            old = self._vessels.get(mmsi)
            if old and name is None:
                v["name"] = old.get("name")
            if old and ship_type is None:
                v["ship_type"] = old.get("ship_type")
            v = describe(v, self.lat, self.lon, self.cfg.approach_min_knots)
            if v["distance_m"] > self.cfg.radius_m:
                self._vessels.pop(mmsi, None)
                return None
            self._vessels[mmsi] = v
        return v

    def name(self, mmsi: int, name: str, ship_type: int | None = None) -> None:
        with self._lock:
            if (v := self._vessels.get(mmsi)) is not None:
                v["name"] = name
                if ship_type is not None:
                    v["ship_type"] = ship_type

    # ---- reports out ----
    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            for mmsi in [m for m, v in self._vessels.items() if now - v["at"] > self.cfg.stale_s]:
                del self._vessels[mmsi]
            vessels = sorted((dict(v) for v in self._vessels.values()), key=lambda v: v["distance_m"])
        approaching = [v for v in vessels if v["approaching"]]
        return {
            "driver": self.cfg.driver,
            "ok": self.ok,
            "error": self.error,
            "configured": self.configured,
            "last_message_at": self.last_message_at,
            "messages": self.messages,
            "bridge": {"lat": self.lat, "lon": self.lon, "radius_m": self.cfg.radius_m},
            "vessels": vessels,
            "approaching": approaching[0] if approaching else None,
        }

    # ---- driver thread ----
    def start(self) -> None:
        if self.cfg.driver == "aisstream":
            self._thread = threading.Thread(target=self._run_aisstream, name="ais", daemon=True)
            self._thread.start()
        elif self.cfg.driver != "none":
            self.error = f"unknown ais.driver {self.cfg.driver!r}"
            log.error(self.error)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _bbox(self) -> list[list[float]]:
        """Bounding box ``radius_m`` around the bridge: [[lat, lon], [lat, lon]]."""
        dlat = math.degrees(self.cfg.radius_m / EARTH_M)
        dlon = dlat / max(0.1, math.cos(math.radians(self.lat)))
        return [[round(self.lat - dlat, 5), round(self.lon - dlon, 5)], [round(self.lat + dlat, 5), round(self.lon + dlon, 5)]]

    def _run_aisstream(self) -> None:
        import asyncio

        key = os.environ.get("WTH_AISSTREAM_KEY", "")
        if not key:
            self.error = "WTH_AISSTREAM_KEY not set (get a free key at aisstream.io)"
            log.warning("ais: %s", self.error)
            return
        if not self.configured:
            self.error = "site.bridge_lat / bridge_lon not set"
            log.warning("ais: %s", self.error)
            return
        try:
            import websockets
        except ImportError:
            self.error = "websockets package missing (pip install -e .[ais] or apt install python3-websockets)"
            log.warning("ais: %s", self.error)
            return

        async def session() -> None:
            sub = {"APIKey": key, "BoundingBoxes": [self._bbox()],
                   "FilterMessageTypes": ["PositionReport", "ShipStaticData", "StandardClassBPositionReport"]}
            async with websockets.connect(self.cfg.url, ping_interval=20) as ws:
                await ws.send(json.dumps(sub))
                self.ok, self.error = True, None
                log.info("ais: connected to %s", self.cfg.url)
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        self.handle_aisstream(json.loads(raw))
                    except Exception as exc:  # one bad message must not drop the stream
                        log.debug("ais: bad message %s: %s", raw[:200], exc)

        async def loop() -> None:
            while not self._stop.is_set():
                try:
                    await session()
                except Exception as exc:
                    self.ok, self.error = False, f"{type(exc).__name__}: {exc}"
                    log.warning("ais: %s; retrying in %.0f s", self.error, self.cfg.retry_s)
                    await asyncio.sleep(self.cfg.retry_s)

        asyncio.run(loop())

    def handle_aisstream(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """One aisstream.io JSON message."""
        if "error" in msg:
            self.ok, self.error = False, str(msg["error"])
            return None
        meta = msg.get("MetaData") or {}
        kind = msg.get("MessageType")
        body = (msg.get("Message") or {}).get(kind) or {}
        mmsi = int(meta.get("MMSI") or body.get("UserID") or 0)
        if not mmsi:
            return None
        name = (meta.get("ShipName") or "").strip() or None
        if kind == "ShipStaticData":
            self.name(mmsi, (body.get("Name") or name or "").strip() or f"MMSI {mmsi}", body.get("Type"))
            return None
        if kind not in ("PositionReport", "StandardClassBPositionReport"):
            return None
        lat, lon = body.get("Latitude", meta.get("latitude")), body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return None
        sog = body.get("Sog")
        cog = body.get("Cog")
        hdg = body.get("TrueHeading")
        return self.report(mmsi, float(lat), float(lon),
                           None if sog is None or sog >= 102.3 else float(sog),
                           None if cog is None or cog >= 360 else float(cog),
                           None if hdg is None or hdg >= 511 else float(hdg),
                           name=name)
