"""Where the detectors look: the staged attention flow.

Instead of measuring everything on every frame, the pipeline works in three stages:

1. ``traffic``: only the lane and deck boxes are measured. Once no lane has moved for
   ``stopped_s`` (both directions stopped, or the lanes can't be read), go to ``watch``.
2. ``watch``: also measure the leaves and look for boats. A leaf moving or up -> ``track``.
   Cars moving again for ``resume_s`` -> ``traffic``. No leaf movement for ``quiet_s`` ->
   ``traffic`` as well, so a stop for a red light or an accident bounces between the two.
   A boat (camera or AIS) pushes that deadline out to ``boat_quiet_s`` after the last
   sighting: a boat in the river is the best sign an opening is coming.
3. ``track``: the bridge is moving or up. Leaves every frame, the boat model at its full
   rate. All leaves flat for ``flat_s`` -> back to ``watch`` (and from there, once cars
   move, to ``traffic``). Leaves unreadable for ``lost_s`` -> ``watch``.

Pure like ``Pipeline``: no threads, no clock, so ``wth-edge eval`` replays it. Without lane
boxes there is no traffic to watch, so the machine stays in ``watch`` / ``track``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STAGES = ("traffic", "watch", "track")
# What a stage measures. The Pipeline reads these.
LOOKS_AT_LEAVES = ("watch", "track")
LOOKS_FOR_BOATS = ("watch", "track")


@dataclass
class StageInputs:
    """What the stage machine sees on one frame."""

    # Per lane: True moving, False stopped, None not readable. Empty = no lane boxes.
    lanes_moving: dict[str, bool | None]
    # "active" (a leaf opening, open or closing), "flat" (all readable leaves closed),
    # or None (no leaf readable, or leaves not measured this stage).
    leaves: str | None = None
    # A camera boat crossing in progress, or AIS reports one approaching.
    boat: bool = False


class StageMachine:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.stage = "traffic"
        self.since: float | None = None
        self.reason: str | None = None
        self._stopped_since: float | None = None
        self._moving_since: float | None = None
        self._flat_since: float | None = None
        self._lost_since: float | None = None
        self._quiet_until: float | None = None
        self.boat_seen = False

    # ---- helpers ----
    def _traffic_clocks(self, t: float, lanes: dict[str, bool | None]) -> None:
        any_moving = any(m is True for m in lanes.values())
        if any_moving:
            self._stopped_since = None
            if self._moving_since is None:
                self._moving_since = t
        else:
            self._moving_since = None
            if self._stopped_since is None:
                self._stopped_since = t

    def _enter(self, t: float, stage: str, reason: str) -> dict[str, Any]:
        prev = self.stage
        self.stage, self.since, self.reason = stage, t, reason
        self._flat_since = self._lost_since = None
        if stage == "watch":
            self._quiet_until = t + self.cfg.quiet_s
            self.boat_seen = False
        if stage == "traffic":
            # The stop has to be seen again, counted from now.
            self._stopped_since = t
        return {"type": "stage", "at": t, "stage": stage, "previous": prev, "reason": reason}

    # ---- the transitions ----
    def update(self, t: float, s: StageInputs) -> list[dict[str, Any]]:
        cfg = self.cfg
        if self.since is None:
            self.since = t
        self._traffic_clocks(t, s.lanes_moving)
        has_lanes = bool(s.lanes_moving)
        lanes_readable = any(m is not None for m in s.lanes_moving.values())

        if self.stage == "traffic":
            if not has_lanes:
                return [self._enter(t, "watch", "no_lanes")]
            if self._stopped_since is not None and t - self._stopped_since >= cfg.stopped_s:
                return [self._enter(t, "watch", "traffic_stopped" if lanes_readable else "traffic_unreadable")]
            return []

        if self.stage == "watch":
            if s.leaves == "active":
                return [self._enter(t, "track", "leaves_moving")]
            if s.boat:
                self.boat_seen = True
                self._quiet_until = max(self._quiet_until or t, t + cfg.boat_quiet_s)
            if not has_lanes or not lanes_readable:
                return []  # nothing to go back to watching
            if self._moving_since is not None and t - self._moving_since >= cfg.resume_s:
                return [self._enter(t, "traffic", "traffic_moving")]
            if self._quiet_until is not None and t >= self._quiet_until:
                return [self._enter(t, "traffic", "bridge_quiet")]
            return []

        # track
        if s.leaves == "active":
            self._flat_since = self._lost_since = None
            return []
        if s.leaves == "flat":
            self._lost_since = None
            if self._flat_since is None:
                self._flat_since = t
            if t - self._flat_since >= cfg.flat_s:
                return [self._enter(t, "watch", "bridge_lowered")]
            return []
        self._flat_since = None
        if self._lost_since is None:
            self._lost_since = t
        if t - self._lost_since >= cfg.lost_s:
            return [self._enter(t, "watch", "leaves_lost")]
        return []

    @property
    def looks_at_leaves(self) -> bool:
        return self.stage in LOOKS_AT_LEAVES

    @property
    def looks_for_boats(self) -> bool:
        return self.stage in LOOKS_FOR_BOATS

    def state(self, t: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"stage": self.stage, "since": self.since, "reason": self.reason,
                               "boat_seen": self.boat_seen}
        if self.stage == "traffic" and self._stopped_since is not None and t is not None:
            out["stopped_for_s"] = round(t - self._stopped_since, 1)
        if self.stage == "watch" and self._quiet_until is not None and t is not None:
            out["quiet_left_s"] = round(max(0.0, self._quiet_until - t), 1)
        return out
