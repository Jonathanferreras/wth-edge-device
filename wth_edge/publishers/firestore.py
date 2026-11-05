"""Writes the device's data to the web app's Firestore over the REST API.

What goes where:

* ``bridgeState/current``: the website fields from ``OpeningMachine.website()``, with
  ``sourceType: "device"``, ``sourceId`` = the device id and a server ``updatedAt``. Only the
  device's own fields are in the update mask, so ``acceptsDeviceUpdates`` (the admin's) is
  never overwritten. While it is ``false`` an admin has taken over: nothing is written and
  the latest state waits until the admin hands back.
* ``events/{id}``: one document per change of position or traffic, in the shape the web
  app's admin form writes, so both sources read the same.
* ``deviceEvents/{id}``: the state machine's phase changes and boat crossings.
* ``devices/{deviceId}``: telemetry and a server ``lastSeenAt``.

Callers never wait on the network: publish_* drop the data in a slot (latest wins for state
and telemetry, a bounded queue for events) and one worker thread writes it, backing off up
to 5 minutes while Firestore or the Wi-Fi is down.

Auth is a service account key (Firebase console > Project settings > Service accounts >
Generate new private key), saved on the Pi as ``publisher.credentials_file``. It uses
``google-auth`` and ``requests``, not the Firebase Admin SDK, which pulls in gRPC.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import Publisher

log = logging.getLogger("wth_edge.publish")

API = "https://firestore.googleapis.com/v1"
SCOPES = ["https://www.googleapis.com/auth/datastore"]
# Detector events worth keeping in the cloud; leaf and lane changes are already in bridgeState.
CLOUD_EVENTS = ("phase", "boat_started", "boat_ended")
STATE_FIELDS = ("position", "positionConfidence", "northBoundTraffic", "northBoundTrafficConfidence",
                "southBoundTraffic", "southBoundTrafficConfidence", "estimatedWaitTime")
MAX_EVENTS = 500
MAX_BACKOFF_S = 300.0


class Timestamp(float):
    """A Unix time to store as a Firestore timestamp rather than a number."""


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def to_value(v: Any) -> dict[str, Any]:
    """A Python value as a Firestore REST ``Value``."""
    if v is None:
        return {"nullValue": None}
    if isinstance(v, Timestamp):
        return {"timestampValue": _rfc3339(v)}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (datetime, date)):
        return {"stringValue": v.isoformat()}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {str(k): to_value(x) for k, x in v.items()}}}
    if isinstance(v, (list, tuple)):
        # Firestore has no arrays of arrays; nested ones are stored as JSON strings.
        return {"arrayValue": {"values": [
            to_value(json.dumps(x, default=str)) if isinstance(x, (list, tuple)) else to_value(x) for x in v]}}
    return {"stringValue": str(v)}


def to_fields(d: dict[str, Any]) -> dict[str, Any]:
    return {str(k): to_value(v) for k, v in d.items()}


def wait_time_doc(wait: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
    """The device's wait estimate in the web app's ``BridgeEstimatedWaitTime`` shape."""
    if not wait:
        return None
    revised = wait.get("revisionReason") == "device_update"
    return {
        "status": wait.get("status", "estimated"),
        "startedAt": Timestamp(wait.get("startedAt") or now),
        "initialTotalMinutes": float(wait.get("initialTotalMinutes") or 0.0),
        "estimatedTotalMinutes": float(wait.get("estimatedTotalMinutes") or 0.0),
        "lastRevisedAt": Timestamp(now) if revised else None,
        "revisionReason": wait.get("revisionReason"),
        "confidence": float(wait.get("confidence") or 0.0),
        "updatedAt": Timestamp(now),
    }


def state_doc(device_id: str, website: dict[str, Any], now: float) -> dict[str, Any]:
    doc = {k: website.get(k) for k in STATE_FIELDS}
    doc["estimatedWaitTime"] = wait_time_doc(website.get("estimatedWaitTime"), now)
    doc.update({"id": "current", "sourceType": "device", "sourceId": device_id})
    return doc


def _session(credentials_file: str):
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    return AuthorizedSession(creds)


def project_from_key(credentials_file: str) -> str:
    try:
        return json.loads(Path(credentials_file).read_text()).get("project_id", "")
    except (OSError, ValueError):
        return ""


class FirestorePublisher(Publisher):
    name = "firestore"

    def __init__(self, cfg, session=None, start: bool = True) -> None:
        super().__init__()
        self.cfg = cfg
        self.project = cfg.project_id or project_from_key(cfg.credentials_file)
        self._session = session
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._state: tuple[str, dict[str, Any]] | None = None
        self._telemetry: tuple[str, dict[str, Any]] | None = None
        self._events: collections.deque = collections.deque(maxlen=MAX_EVENTS)
        self._last_traffic_key: tuple | None = None
        self._backoff = 0.0
        self._retry_at = 0.0
        self._override_checked_at = 0.0
        self.held_by_admin = False
        self.last_error: str | None = None
        self._day = date.today()
        self.writes_today = 0
        self.reads_today = 0
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="firestore", daemon=True)
            self._thread.start()

    # ---- Publisher API (never blocks) ----
    def publish_bridge_state(self, device_id: str, website: dict[str, Any]) -> None:
        with self._lock:
            self._state = (device_id, website)
        self._wake.set()

    def publish_event(self, device_id: str, event: dict[str, Any]) -> None:
        if event.get("type") not in CLOUD_EVENTS:
            return
        with self._lock:
            self._events.append((device_id, event))
        self._wake.set()

    def publish_telemetry(self, device_id: str, telemetry: dict[str, Any]) -> None:
        with self._lock:
            self._telemetry = (device_id, telemetry)
        self._wake.set()

    def status(self) -> dict[str, Any]:
        st = super().status()
        st.update({"project": self.project, "held_by_admin": self.held_by_admin, "error": self.last_error,
                   "writes_today": self.writes_today, "reads_today": self.reads_today,
                   "queued_events": len(self._events)})
        return st

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    # ---- worker ----
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            self.flush()
        self.flush()

    def flush(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now < self._retry_at:
            return
        try:
            self._flush(now)
        except Exception as exc:  # network, auth, quota: back off and keep the data
            self.ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._backoff = min(MAX_BACKOFF_S, max(5.0, self._backoff * 2))
            self._retry_at = now + self._backoff
            log.warning("firestore write failed, retrying in %.0f s: %s", self._backoff, self.last_error)
        else:
            self._backoff, self._retry_at = 0.0, 0.0

    def _flush(self, now: float) -> None:
        if date.today() != self._day:
            self._day, self.writes_today, self.reads_today = date.today(), 0, 0
        with self._lock:
            state, telemetry = self._state, self._telemetry
            events = list(self._events)
        if not (state or telemetry or events):
            return
        if not self.project:
            raise RuntimeError(f"no Firestore project: set publisher.project_id or add {self.cfg.credentials_file}")
        writes: list[dict[str, Any]] = []
        traffic_key = None
        if state is not None and self._may_write_state(now):
            device_id, website = state
            doc = state_doc(device_id, website, now)
            writes.append({
                "update": {"name": self._doc("bridgeState/current"), "fields": to_fields(doc)},
                "updateMask": {"fieldPaths": sorted(doc)},
                "updateTransforms": [{"fieldPath": "updatedAt", "setToServerValue": "REQUEST_TIME"}],
            })
            traffic_key = (doc["position"], doc["northBoundTraffic"], doc["southBoundTraffic"])
            if traffic_key != self._last_traffic_key:
                writes.append(self._create("events", self._history_event(doc, now)))
        for device_id, e in events:
            writes.append(self._create("deviceEvents", self._device_event(device_id, e)))
        if telemetry is not None:
            device_id, tel = telemetry
            writes.append({
                "update": {"name": self._doc(f"devices/{device_id}"),
                           "fields": to_fields({"deviceId": device_id, "telemetry": tel,
                                                "publisher": {"writesToday": self.writes_today,
                                                              "heldByAdmin": self.held_by_admin}})},
                "updateTransforms": [{"fieldPath": "lastSeenAt", "setToServerValue": "REQUEST_TIME"}],
            })
        if not writes:
            return
        self._commit(writes)
        with self._lock:
            if self._state is state and any("bridgeState" in w.get("update", {}).get("name", "") for w in writes):
                self._state = None
            if self._telemetry is telemetry:
                self._telemetry = None
            for _ in events:
                self._events.popleft()
        if traffic_key is not None:
            self._last_traffic_key = traffic_key
        self.ok, self.last_error, self.last_publish_at = True, None, now

    def _may_write_state(self, now: float) -> bool:
        """False while an admin has taken over the website (acceptsDeviceUpdates == false)."""
        if self.held_by_admin and now - self._override_checked_at < self.cfg.override_check_s:
            return False
        self._override_checked_at = now
        resp = self._http().get(self._url("bridgeState/current"),
                                params={"mask.fieldPaths": "acceptsDeviceUpdates"}, timeout=15)
        self.reads_today += 1
        if resp.status_code == 404:
            held = False
        else:
            resp.raise_for_status()
            field = (resp.json().get("fields") or {}).get("acceptsDeviceUpdates") or {}
            held = field.get("booleanValue") is False
        if held != self.held_by_admin:
            log.info("admin override %s", "on: holding device updates" if held else "off: device updates resume")
        self.held_by_admin = held
        return not held

    def _history_event(self, doc: dict[str, Any], now: float) -> dict[str, Any]:
        return {k: doc[k] for k in ("sourceId", "sourceType", "position", "positionConfidence",
                                    "northBoundTraffic", "northBoundTrafficConfidence",
                                    "southBoundTraffic", "southBoundTrafficConfidence")} | {
            "occurredAt": Timestamp(now), "createdAt": Timestamp(now)}

    @staticmethod
    def _device_event(device_id: str, e: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in e.items() if k != "at"}
        out["deviceId"] = device_id
        out["at"] = Timestamp(e.get("at") or time.time())
        return out

    # ---- REST ----
    def _http(self):
        if self._session is None:
            self._session = _session(self.cfg.credentials_file)
        return self._session

    def _doc(self, path: str) -> str:
        return f"projects/{self.project}/databases/(default)/documents/{path}"

    def _url(self, path: str) -> str:
        return f"{API}/{self._doc(path)}"

    def _create(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        doc_id = uuid.uuid4().hex[:20]
        return {"update": {"name": self._doc(f"{collection}/{doc_id}"), "fields": to_fields({"id": doc_id, **data})},
                "currentDocument": {"exists": False}}

    def _commit(self, writes: list[dict[str, Any]]) -> None:
        url = f"{API}/projects/{self.project}/databases/(default)/documents:commit"
        resp = self._http().post(url, json={"writes": writes}, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"commit {resp.status_code}: {resp.text[:300]}")
        self.writes_today += len(writes)
