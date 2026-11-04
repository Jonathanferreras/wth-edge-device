"""The device's current state, shared between processes through one JSON file.

``wth-edge run`` writes it (atomically, so a reader never sees half a file) and the
display service reads it. Keeping the screen in its own process means it can still say
"offline" when the main app has crashed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# Bridge positions match the web app's BRIDGE_POSITION values.
BRIDGE_POSITIONS = ("closed", "opening", "open", "closing", "unknown")


def new_state(device_id: str, version: str) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "version": version,
        "started_at": time.time(),
        "updated_at": time.time(),
        # "starting" | "ok" | "degraded"
        "status": "starting",
        "camera": {"ok": False, "fps": 0.0, "frame_age_s": None, "error": None},
        # Filled by the detectors; "unknown" until they have seen enough.
        "bridge": {"position": "unknown", "confidence": 0.0, "angle_deg": None, "since": None, "line": None},
        "boat": {"present": False, "direction": None, "confidence": 0.0, "since": None, "box": None},
        "far_leaf": {"position": "unknown", "confidence": 0.0, "angle_deg": None, "since": None, "line": None},
        # Per direction: level (light / moderate / heavy / standstill / unknown), confidence, flow, fill.
        "traffic": {},
        "deck": None,
        # The opening state machine: phase, since, confidence, wait, and the website fields.
        "opening": None,
        # Vessels from AIS, when configured.
        "ais": None,
        "detector": None,
        "telemetry": {},
        "publisher": {"driver": None, "ok": None, "last_publish_at": None},
        "recording": {"available": False, "recording": False, "label": None, "elapsed_s": None, "trigger": None},
        "web_url": None,
    }


def write_state(path: Path | str, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.time()
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_state(path: Path | str) -> dict[str, Any] | None:
    try:
        with Path(path).open() as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return None
    return data if isinstance(data, dict) else None


def state_age_s(state: dict[str, Any] | None, now: float | None = None) -> float | None:
    if not state or "updated_at" not in state:
        return None
    return (now if now is not None else time.time()) - float(state["updated_at"])
