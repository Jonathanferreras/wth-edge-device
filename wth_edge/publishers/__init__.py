"""Where device data goes. Detectors and telemetry only talk to a Publisher.

``console`` logs each publish; ``firestore`` (``publishers/firestore.py``) writes the web
app's ``bridgeState/current``, ``events``, ``deviceEvents`` and ``devices/{id}``. Which
bridge states are worth publishing is decided before this, in ``publishers/policy.py``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("wth_edge.publish")


class Publisher:
    name = "base"

    def __init__(self) -> None:
        self.last_publish_at: float | None = None
        self.ok: bool | None = None

    def publish_telemetry(self, device_id: str, telemetry: dict[str, Any]) -> None:
        raise NotImplementedError

    def publish_event(self, device_id: str, event: dict[str, Any]) -> None:
        """A detector event: phase change, leaf position, traffic level, boat started / ended."""
        raise NotImplementedError

    def publish_bridge_state(self, device_id: str, website: dict[str, Any]) -> None:
        """The website's ``bridgeState/current`` fields, whenever they change."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"driver": self.name, "ok": self.ok, "last_publish_at": self.last_publish_at}


class ConsolePublisher(Publisher):
    """Logs one JSON line per publish. Useful on a bench Pi and on a laptop."""

    name = "console"

    def publish_telemetry(self, device_id: str, telemetry: dict[str, Any]) -> None:
        log.info("telemetry %s", json.dumps({"device_id": device_id, **telemetry}, separators=(",", ":")))
        self.last_publish_at = time.time()
        self.ok = True

    def publish_event(self, device_id: str, event: dict[str, Any]) -> None:
        log.info("event %s", json.dumps({"device_id": device_id, **event}, separators=(",", ":"), default=str))
        self.last_publish_at = time.time()
        self.ok = True

    def publish_bridge_state(self, device_id: str, website: dict[str, Any]) -> None:
        log.info("bridge_state %s", json.dumps({"device_id": device_id, **website}, separators=(",", ":"), default=str))
        self.last_publish_at = time.time()
        self.ok = True


def make_publisher(cfg) -> Publisher:
    driver = cfg.driver.lower()
    if driver == "console":
        return ConsolePublisher()
    if driver == "firestore":
        from .firestore import FirestorePublisher

        return FirestorePublisher(cfg)
    raise ValueError(f"unknown publisher.driver {cfg.driver!r} (console or firestore)")
