"""Where device data goes. Detectors and telemetry only talk to a Publisher.

Only ``console`` exists in this first step. A ``firestore`` publisher (writing
``bridgeState/current``, ``events`` and ``devices/{id}``) comes with the backend phase.
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


def make_publisher(cfg) -> Publisher:
    driver = cfg.driver.lower()
    if driver == "console":
        return ConsolePublisher()
    raise ValueError(f"unknown publisher.driver {cfg.driver!r} (only 'console' so far)")
