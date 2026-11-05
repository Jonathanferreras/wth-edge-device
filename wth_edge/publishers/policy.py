"""When the website's bridge state is worth a write.

Every write to ``bridgeState/current`` costs one read per open browser tab, so the device
writes only what a visitor would notice:

* a new ``position`` (or the wait estimate appearing, going or restarting) goes out at once;
* a new traffic level goes out once it has held for ``traffic_hold_s``, and no sooner than
  ``min_interval_s`` after the last write;
* confidences and the wait countdown ride along but never cause a write on their own (the
  website counts down from ``startedAt`` and ``estimatedTotalMinutes`` itself);
* the unchanged state is rewritten every ``heartbeat_s`` so the website can tell the device
  is alive.

``withhold`` sends a field as unknown until its detector has passed the accuracy gate.
Pure, like ``Pipeline``: the caller passes the time in.
"""

from __future__ import annotations

import copy
from typing import Any

UNKNOWN_TRAFFIC = ("northBoundTraffic", "southBoundTraffic")


def apply_withhold(website: dict[str, Any], withhold: tuple[str, ...] | list[str]) -> dict[str, Any]:
    out = copy.deepcopy(website)
    if "position" in withhold:
        out["position"], out["positionConfidence"] = "unknown", 0.0
        out["estimatedWaitTime"] = None
    if "traffic" in withhold:
        for f in UNKNOWN_TRAFFIC:
            out[f], out[f + "Confidence"] = "unknown", 0.0
    if "wait" in withhold:
        out["estimatedWaitTime"] = None
    return out


def _wait_key(w: dict[str, Any] | None) -> tuple | None:
    if not w:
        return None
    return (w.get("status"), round(float(w.get("startedAt") or 0)), w.get("initialTotalMinutes"))


class BridgeStatePolicy:
    def __init__(self, traffic_hold_s: float = 60.0, min_interval_s: float = 30.0,
                 heartbeat_s: float = 600.0, withhold: tuple[str, ...] | list[str] = ()) -> None:
        self.traffic_hold_s = traffic_hold_s
        self.min_interval_s = min_interval_s
        self.heartbeat_s = heartbeat_s
        self.withhold = tuple(withhold)
        self._sent: dict[str, Any] | None = None
        self._sent_at: float | None = None
        self._traffic_candidate: tuple | None = None
        self._traffic_since: float = 0.0

    def offer(self, now: float, website: dict[str, Any]) -> dict[str, Any] | None:
        """The state to write now, or None. A returned state counts as written."""
        site = apply_withhold(website, self.withhold)
        if self._sent is None:
            return self._send(now, site)
        last = self._sent
        urgent = (site["position"] != last["position"]
                  or _wait_key(site.get("estimatedWaitTime")) != _wait_key(last.get("estimatedWaitTime")))
        if urgent:
            return self._send(now, site)
        traffic = tuple(site[f] for f in UNKNOWN_TRAFFIC)
        if traffic != tuple(last[f] for f in UNKNOWN_TRAFFIC):
            if traffic != self._traffic_candidate:
                self._traffic_candidate, self._traffic_since = traffic, now
            if now - self._traffic_since >= self.traffic_hold_s and now - self._sent_at >= self.min_interval_s:
                return self._send(now, site)
            return None
        self._traffic_candidate = None
        if now - self._sent_at >= self.heartbeat_s:
            return self._send(now, site)
        return None

    def _send(self, now: float, site: dict[str, Any]) -> dict[str, Any]:
        self._sent, self._sent_at, self._traffic_candidate = site, now, None
        return site
