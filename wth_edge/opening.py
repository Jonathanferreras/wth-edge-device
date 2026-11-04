"""The bridge opening state machine.

Signals in (traffic per direction, deck, leaf angles, camera boat, AIS), one phase out,
plus what the website shows for it: ``position``, traffic per direction and the wait
estimate. Pure: no threads, no clock of its own, so ``wth-edge eval`` replays it on clips.

Phases, from the design doc "Bridge opening state machine":

    flowing -> gates_down -> lifting -> raised -> lowering -> releasing -> flowing

* ``gates_down``: both directions at a standstill with the deck clear. Stopped traffic
  starts the clock; a boat (camera or AIS) or the leaves confirm it. Without either for
  ``false_alarm_s`` it was an accident or a light, and we go back to ``flowing``.
* ``lifting`` / ``lowering``: a leaf is moving. The leaves lift one at a time (far leaf
  first), so the phase follows the highest leaf for "moving" and needs all visible leaves
  up for ``raised``. A boat often passes while the near leaf is still rising, so
  ``lifting`` straight to ``lowering`` is a normal path.
* ``releasing``: leaves flat, gates up, the queue clearing: heavy traffic for a while.
* ``unknown``: nothing readable for ``unknown_after_s``.

The website never requires a boat. Where the camera cannot see an approach (the site's
``approach_sides``), a missing boat is not held against the opening.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PHASES = ("flowing", "gates_down", "lifting", "raised", "lowering", "releasing", "unknown")
# Phases during which the website shows a wait time.
WAIT_PHASES = ("gates_down", "lifting", "raised", "lowering")
POSITION_FOR = {
    "flowing": "closed", "gates_down": "closed", "lifting": "opening", "raised": "open",
    "lowering": "closing", "releasing": "closed", "unknown": "unknown",
}
HISTORY_FILE = "openings.json"


@dataclass
class Signals:
    """One snapshot of what the detectors see. ``None`` means "not measured"."""

    northbound: str = "unknown"          # traffic level
    northbound_confidence: float = 0.0
    southbound: str = "unknown"
    southbound_confidence: float = 0.0
    northbound_moving: bool | None = None
    southbound_moving: bool | None = None
    deck_clear: bool | None = None
    deck_moving: bool | None = None
    # Per visible leaf: angle in degrees, degrees per second, and the leaf tracker's own
    # position (closed / opening / open / closing), which already has a hold time.
    leaf_angles: dict[str, float] = field(default_factory=dict)
    leaf_rates: dict[str, float] = field(default_factory=dict)
    leaf_positions: dict[str, str] = field(default_factory=dict)
    boat_present: bool = False
    boat_direction: str | None = None   # "e2w" / "w2e"
    boat_confidence: float = 0.0
    # From AIS: side the nearest approaching vessel comes from and its ETA in seconds.
    ais_side: str | None = None
    ais_eta_s: float | None = None
    ais_name: str | None = None


class OpeningHistory:
    """Durations of past openings, per phase, for the wait estimate."""

    def __init__(self, path: Path | str | None, keep: int, defaults: dict[str, float]) -> None:
        self.path = Path(path) if path else None
        self.keep = keep
        self.defaults = defaults
        self.openings: list[dict[str, Any]] = []
        if self.path and self.path.is_file():
            try:
                self.openings = json.loads(self.path.read_text())[-keep:]
            except (OSError, ValueError) as exc:
                log.warning("ignoring %s: %s", self.path, exc)

    def typical(self, phase: str) -> float:
        seen = [o["phases"][phase] for o in self.openings if o.get("phases", {}).get(phase)]
        return statistics.median(seen) if seen else self.defaults[phase]

    def add(self, opening: dict[str, Any]) -> None:
        self.openings = (self.openings + [opening])[-self.keep:]
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.openings, indent=1))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot save %s: %s", self.path, exc)


class OpeningMachine:
    def __init__(self, cfg, bridge_cfg, approach_sides=("east",), history: OpeningHistory | None = None) -> None:
        self.cfg = cfg
        self.bridge_cfg = bridge_cfg
        self.approach_sides = tuple(s.lower() for s in approach_sides)
        self.history = history or OpeningHistory(None, cfg.history, self._defaults())
        self.phase = "unknown"
        self.since: float | None = None
        self.confidence = 0.0
        self._candidate: str | None = None
        self._candidate_since = 0.0
        self._last_signal: float | None = None
        self._opening: dict[str, Any] | None = None   # the opening in progress
        self._boat_seen_in_opening = False
        self.boat_direction: str | None = None
        self.boat_source: str | None = None
        self.wait: dict[str, Any] | None = None
        self.last: Signals = Signals()

    def _defaults(self) -> dict[str, float]:
        c = self.cfg
        return {"gates_down": c.default_gates_down_s, "lifting": c.default_lifting_s, "raised": c.default_raised_s,
                "lowering": c.default_lowering_s, "releasing": c.default_releasing_s}

    # ---- leaf summary ----
    def _leaves(self, s: Signals) -> tuple[str | None, float]:
        """("flat" | "moving_up" | "moving_down" | "up" | "mid", highest angle)."""
        if not s.leaf_angles:
            return None, 0.0
        b = self.bridge_cfg
        angles = list(s.leaf_angles.values())
        top = max(angles)
        # The leaf trackers decide "moving" (they smooth and hold); a rate alone flickers
        # when the Hough line jumps about inside the closed zone.
        positions = [s.leaf_positions.get(k) for k in s.leaf_angles]
        if positions and all(p is not None for p in positions):
            if "opening" in positions:
                return "moving_up", top
            if "closing" in positions:
                return "moving_down", top
            if all(p == "closed" for p in positions):
                return "flat", top
            if all(p == "open" for p in positions):
                return "up", top
            return "mid", top
        rates = [s.leaf_rates.get(k, 0.0) for k in s.leaf_angles]
        if top > b.closed_max_deg and any(r >= b.min_rate_deg_s for r in rates):
            return "moving_up", top
        if top > b.closed_max_deg and any(r <= -b.min_rate_deg_s for r in rates):
            return "moving_down", top
        if all(a <= b.closed_max_deg for a in angles):
            return "flat", top
        if all(a >= b.open_min_deg for a in angles):
            return "up", top
        return "mid", top

    # ---- the transitions ----
    def _target(self, t: float, s: Signals) -> tuple[str, float]:
        """The phase the signals point at, and how long they must hold."""
        cfg = self.cfg
        leaves, _ = self._leaves(s)
        both_still = s.northbound == "standstill" and s.southbound == "standstill"
        deck_ok = s.deck_clear is None or s.deck_clear
        either_moving = bool(s.northbound_moving) or bool(s.southbound_moving) or bool(s.deck_moving)
        boat = s.boat_present or s.ais_eta_s is not None

        if leaves == "moving_up":
            return "lifting", self.bridge_cfg.hold_s
        if leaves == "moving_down":
            return "lowering", self.bridge_cfg.hold_s
        if self.phase in ("flowing", "unknown", "releasing"):
            if leaves == "mid" or leaves == "up":
                # Leaves already up when we started looking, or a lift we missed.
                return ("raised" if leaves == "up" else "lifting"), cfg.raised_hold_s
            if both_still and deck_ok and self.phase != "releasing":
                return "gates_down", cfg.gates_down_hold_s
            if self.phase == "releasing":
                if either_moving:
                    return "flowing", cfg.release_hold_s
                return "releasing", 0.0
            if self.phase == "unknown" and (s.northbound != "unknown" or s.southbound != "unknown" or leaves):
                return "flowing", 0.0
            return self.phase, 0.0
        if self.phase == "gates_down":
            if leaves == "up":
                return "raised", cfg.raised_hold_s
            if leaves == "mid":
                return "lifting", self.bridge_cfg.hold_s
            if self.since is not None and t - self.since >= cfg.false_alarm_s and not boat and not self._boat_seen_in_opening:
                return "flowing", 0.0
            if either_moving and not boat and leaves in (None, "flat"):
                # Traffic moving again with nothing else seen: the stop was not for a boat.
                return "flowing", cfg.release_hold_s
            return "gates_down", 0.0
        if self.phase == "lifting":
            if leaves == "up":
                return "raised", cfg.raised_hold_s
            if leaves == "flat":
                return "releasing", cfg.flat_hold_s
            return "lifting", 0.0
        if self.phase == "raised":
            if leaves == "flat":
                return "releasing", cfg.flat_hold_s
            return "raised", 0.0
        if self.phase == "lowering":
            if leaves == "flat":
                return "releasing", cfg.flat_hold_s
            if leaves == "up":
                return "raised", cfg.raised_hold_s
            return "lowering", 0.0
        return self.phase, 0.0

    def update(self, t: float, s: Signals) -> list[dict[str, Any]]:
        """Feed one snapshot. Returns phase-change events (and boat events the machine infers)."""
        self.last = s
        events: list[dict[str, Any]] = []
        readable = s.northbound != "unknown" or s.southbound != "unknown" or bool(s.leaf_angles)
        if readable:
            self._last_signal = t
        if s.boat_present and self.phase != "flowing":
            self._boat_seen_in_opening = True
            if s.boat_direction and self.boat_direction is None:
                self.boat_direction, self.boat_source = s.boat_direction, "camera"
        if self.boat_direction is None and s.ais_side:
            self.boat_direction = "e2w" if s.ais_side == "east" else "w2e"
            self.boat_source = "ais"

        if self._last_signal is None or t - self._last_signal > self.cfg.unknown_after_s:
            target, hold = "unknown", 0.0
        else:
            target, hold = self._target(t, s)

        if target != self.phase:
            if target != self._candidate:
                self._candidate, self._candidate_since = target, t
            if t - self._candidate_since >= hold:
                events.append(self._enter(t, target))
        else:
            self._candidate = None
        self.confidence = self._confidence(s)
        self.wait = self._wait(t)
        return events

    def _enter(self, t: float, phase: str) -> dict[str, Any]:
        prev, prev_since = self.phase, self.since
        self.phase, self.since, self._candidate = phase, t, None
        if prev in WAIT_PHASES + ("releasing",) and self._opening is not None and prev_since is not None:
            self._opening["phases"][prev] = round(t - prev_since, 1)
        if phase == "gates_down" or (phase in ("lifting", "raised") and self._opening is None):
            self._opening = {"started_at": t, "phases": {}, "from": prev}
            self._boat_seen_in_opening = False
            self.boat_direction = self.boat_source = None
        if phase == "flowing" and self._opening is not None:
            done = self._opening
            self._opening = None
            if any(p in done["phases"] for p in ("lifting", "raised", "lowering")):
                done["ended_at"] = t
                done["boat_direction"], done["boat_source"] = self.boat_direction, self.boat_source
                self.history.add(done)
        return {"type": "phase", "at": t, "phase": phase, "previous": prev, "position": POSITION_FOR[phase],
                "boat_direction": self.boat_direction, "boat_source": self.boat_source}

    # ---- outputs ----
    def _confidence(self, s: Signals) -> float:
        if self.phase == "unknown":
            return 0.0
        if self.phase == "flowing":
            return round(max(0.5, (s.northbound_confidence + s.southbound_confidence) / 2), 2)
        if self.phase == "gates_down":
            # Stopped traffic alone is "likely"; a boat makes it "high", unless the only
            # approach we could have seen it on is a blind side.
            if self._boat_seen_in_opening or s.ais_eta_s is not None:
                return 0.8
            return 0.6
        if self.phase in ("lifting", "raised", "lowering"):
            return 0.9 if self._boat_seen_in_opening else 0.8
        return 0.7  # releasing

    def _wait(self, t: float) -> dict[str, Any] | None:
        if self.phase not in WAIT_PHASES or self.since is None or self._opening is None:
            return None
        order = list(WAIT_PHASES)
        i = order.index(self.phase)
        remaining = max(0.0, self.history.typical(self.phase) - (t - self.since))
        remaining += sum(self.history.typical(p) for p in order[i + 1:])
        remaining += self.history.typical("releasing")
        initial = sum(self.history.typical(p) for p in WAIT_PHASES + ("releasing",))
        started = self._opening["started_at"]
        return {
            "status": "estimated",
            "startedAt": started,
            "initialTotalMinutes": round(initial / 60, 1),
            "estimatedTotalMinutes": round(((t - started) + remaining) / 60, 1),
            "remainingMinutes": round(remaining / 60, 1),
            "revisionReason": "initial_prediction" if self.phase == "gates_down" else "device_update",
            "confidence": round(min(0.9, 0.5 + 0.1 * len(self.history.openings)), 2),
        }

    def website(self) -> dict[str, Any]:
        """The fields the web app's ``bridgeState/current`` takes, invariants included."""
        s = self.last
        position = POSITION_FOR[self.phase]

        def traffic(level: str, conf: float) -> tuple[str, float]:
            if position != "closed":
                return ("standstill" if level != "unknown" else "unknown"), conf
            return level, conf

        nb, nbc = traffic(s.northbound, s.northbound_confidence)
        sb, sbc = traffic(s.southbound, s.southbound_confidence)
        if self.phase == "releasing":
            nb = "heavy" if nb != "unknown" else nb
            sb = "heavy" if sb != "unknown" else sb
        return {
            "position": position,
            "positionConfidence": self.confidence,
            "northBoundTraffic": nb,
            "northBoundTrafficConfidence": round(nbc, 2),
            "southBoundTraffic": sb,
            "southBoundTrafficConfidence": round(sbc, 2),
            "estimatedWaitTime": self.wait if position in ("opening", "open", "closing") else None,
        }

    def state(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "since": self.since,
            "confidence": self.confidence,
            "position": POSITION_FOR[self.phase],
            "boat_direction": self.boat_direction,
            "boat_source": self.boat_source,
            "wait": self.wait,
            "openings_seen": len(self.history.openings),
            "website": self.website(),
        }
