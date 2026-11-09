"""Detectors: leaf angles, traffic per lane, boat crossings, and the opening state machine.

What gets measured depends on the stage (``stages.py``): traffic only while cars flow,
then the leaves and boats once traffic has stopped, then everything at full rate while
the bridge moves. ``[stages] enabled = false`` measures everything on every frame.

``Pipeline`` is frame in, events and state out, with no threads or camera, so the live
device and ``wth-edge eval`` run exactly the same code. ``DetectorRunner`` feeds it from
the FrameBus in a thread and hands events to listeners (auto-recording, the publisher).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)

Listener = Callable[[list[dict[str, Any]], dict[str, Any]], None]


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


LANES = ("northbound", "southbound")
# Leaf boxes: "bridge" is the near leaf (the old single box), "far_leaf" the other one.
LEAVES = {"bridge": "near", "far_leaf": "far"}
# Boat boxes: "boat" is the channel and the river beyond, "boat_near" the stretch of river
# close to the camera that the main box can't cover without taking in the trees. Each gets
# its own motion gate and model run; the best sighting of the frame feeds one tracker.
BOAT_ROIS = ("boat", "boat_near")


def _best(sightings):
    found = [s for s in sightings if s is not None]
    return max(found, key=lambda s: s.confidence) if found else None


class Pipeline:
    def __init__(self, config, rois: dict | None = None, repo_root=None, history=None) -> None:
        from ..config import REPO_ROOT
        from ..opening import OpeningMachine
        from .boat import BoatTracker, MotionGate, YoloBoat
        from .bridge import BridgeTracker
        from .stages import StageMachine
        from .traffic import DeckTracker, LaneTracker, LaneWatcher

        self.config = config
        # The live dict: ROIs saved from the web panel apply on the next frame.
        self.rois = rois if rois is not None else config.site.rois
        bcfg, boat, tcfg = config.bridge_detector, config.boat_detector, config.traffic_detector
        self.bridge = BridgeTracker(bcfg) if bcfg.enabled else None
        self.far_leaf = BridgeTracker(bcfg) if bcfg.enabled else None
        self.lanes = {name: LaneTracker(tcfg) for name in LANES} if tcfg.enabled else {}
        self.watchers = {name: LaneWatcher(tcfg) for name in (*LANES, "deck")} if tcfg.enabled else {}
        self.deck = DeckTracker(tcfg) if tcfg.enabled else None
        self.stages = StageMachine(config.stages) if config.stages.enabled else None
        self.machine = OpeningMachine(config.opening, bcfg, config.site.approach_sides, history) if config.opening.enabled else None
        self.motion = {name: MotionGate(boat) for name in BOAT_ROIS} if boat.enabled else {}
        self.yolo = None
        self.yolo_error: str | None = None
        if boat.enabled and boat.confirm == "yolo":
            try:
                self.yolo = YoloBoat(boat, repo_root or REPO_ROOT)
            except Exception as exc:  # missing model or an OpenCV without DNN
                self.yolo_error = str(exc)
                log.warning("boat model unavailable (%s); confirming boats by motion only", exc)
        self.boats = BoatTracker(boat, config.site.left_is, config.site.right_is,
                                 require_travel=self.yolo is None) if boat.enabled else None
        self._last_yolo = 0.0
        self._last_motion_at: float | None = None
        self._box_motion_at: dict[str, float] = {}
        self._crossing_box: str | None = None   # the box the current crossing was last seen in
        self._idle_turn = 0
        self.last_ms: float = 0.0
        self.last_t: float | None = None

    def process(self, t: float, frame: np.ndarray, hires=None, ais: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """``frame``: the lores RGB frame. ``hires``: an optional ``HiRes`` full-res frame,
        which the bridge (thin edges) and the boat model (small boats) both need. ``ais``:
        the latest ``AisSource.snapshot()``, when there is one."""
        from .bridge import measure_angle

        t0 = time.perf_counter()
        self.last_t = t
        events: list[dict[str, Any]] = []
        rois = self.rois
        leaves_on = self.stages is None or self.stages.looks_at_leaves
        boats_on = self.stages is None or self.stages.looks_for_boats
        tracking = self.stages is not None and self.stages.stage == "track"
        for name, which in LEAVES.items():
            if not leaves_on:
                break
            tracker = self.bridge if which == "near" else self.far_leaf
            if tracker is None or name not in rois:
                continue
            before = tracker.position
            src = hires.grey() if hires is not None else frame
            changed = tracker.update(t, measure_angle(src, rois[name], self.config.bridge_detector))
            if changed:
                events.append({"type": "bridge", "at": t, "leaf": which, "position": changed, "previous": before,
                               "angle_deg": tracker.state()["angle_deg"]})
        for name, watcher in self.watchers.items():
            if name not in rois:
                continue
            reading = watcher.update(frame, rois[name])
            if name == "deck":
                still = [self.lanes[n].level == "standstill" for n in LANES if n in rois]
                self.deck.update(t, reading, watcher.vehicles, bool(still) and all(still))
            else:
                before = self.lanes[name].level
                changed = self.lanes[name].update(t, reading)
                if changed:
                    events.append({"type": "traffic", "at": t, "direction": name, "level": changed, "previous": before})
        boat_rois = [name for name in BOAT_ROIS if name in rois]
        if self.boats is not None and boat_rois:
            cfg = self.config.boat_detector
            # The motion gates are cheap and keep learning the river in every stage, so they
            # don't fire on a stale background when the watch starts.
            seen = {name: self.motion[name].update(frame, rois[name]) for name in boat_rois}
            for name, sighting in seen.items():
                if sighting is not None:
                    self._box_motion_at[name] = t
            moving = _best(seen.values())
            if moving is not None:
                self._last_motion_at = t
            if not boats_on:
                events += self.boats.tick(t)  # only let a crossing in progress end
            elif self.yolo is not None:
                # Motion or an open crossing runs the model often; otherwise it still looks
                # now and then, because a slow tug fades into the motion background.
                busy = (tracking or (self._last_motion_at is not None and t - self._last_motion_at < 2.0)
                        or self.boats.current is not None)
                hz = cfg.yolo_max_hz if busy else cfg.yolo_idle_hz
                if hz > 0 and t - self._last_yolo >= 1.0 / hz:
                    self._last_yolo = t
                    src = hires.rgb() if hires is not None else frame
                    events += self.boats.update(t, self._yolo_look(t, src, boat_rois))
                else:
                    events += self.boats.tick(t)
            else:
                events += self.boats.update(t, moving)
        if self.stages is not None:
            events += self.stages.update(t, self._stage_inputs(ais))
        if self.machine is not None:
            events += self.machine.update(t, self.signals(ais))
        self.last_ms = (time.perf_counter() - t0) * 1000
        return events

    def _yolo_look(self, t: float, src, boat_rois: list[str]):
        """One model look. The model is the expensive part (~0.7 s a box on a Pi 5), so it
        runs on the boxes where something just moved and the box holding the crossing; with
        nothing going on it takes the boxes in turn."""
        names = [n for n in boat_rois if t - self._box_motion_at.get(n, -1e9) < 2.0]
        if self.boats.current is not None and self._crossing_box in boat_rois and self._crossing_box not in names:
            names.append(self._crossing_box)
        if not names:
            names = [boat_rois[self._idle_turn % len(boat_rois)]]
            self._idle_turn += 1
        best, best_name = None, None
        for name in names:
            s = self.yolo.detect(src, self.rois[name])
            if s is not None and (best is None or s.confidence > best.confidence):
                best, best_name = s, name
        if best is not None:
            self._crossing_box = best_name
        return best

    def _stage_inputs(self, ais: dict[str, Any] | None = None):
        from .stages import StageInputs

        lanes = {name: (tr.moving if tr.level != "unknown" else None) for name, tr in self.lanes.items()
                 if name in self.rois}
        leaves = None
        if self.stages is not None and self.stages.looks_at_leaves:
            positions = [tr.position for tr in (self.bridge, self.far_leaf)
                         if tr is not None and tr.position != "unknown"]
            if any(p in ("opening", "open", "closing") for p in positions):
                leaves = "active"
            elif positions:
                leaves = "flat"
        boat = (self.boats is not None and self.boats.present) or bool(ais and ais.get("approaching"))
        return StageInputs(lanes, leaves, boat)

    def signals(self, ais: dict[str, Any] | None = None):
        """What the state machine sees, built from the trackers."""
        from ..opening import Signals

        s = Signals()
        for name, tr in self.lanes.items():
            setattr(s, name, tr.level)
            setattr(s, f"{name}_confidence", tr.confidence)
            setattr(s, f"{name}_moving", tr.moving)
        if self.deck is not None:
            s.deck_clear, s.deck_moving = self.deck.clear, self.deck.moving
        for which, tr in (("near", self.bridge), ("far", self.far_leaf)):
            if self.stages is not None and not self.stages.looks_at_leaves:
                break  # not watching the leaves: their last reading is stale
            if tr is not None and tr.angle is not None and tr.position != "unknown":
                s.leaf_angles[which] = tr.angle
                s.leaf_rates[which] = tr.rate
                s.leaf_positions[which] = tr.position
        if self.boats is not None:
            b = self.boats.state()
            s.boat_present, s.boat_direction, s.boat_confidence = b["present"], b["direction"], b["confidence"]
        if ais and (v := ais.get("approaching")):
            s.ais_side, s.ais_eta_s, s.ais_name = v.get("side"), v.get("eta_s"), v.get("name")
        return s

    def _leaf_state(self, tr) -> dict[str, Any]:
        if tr is None:
            return {"position": "unknown", "confidence": 0.0}
        st = tr.state()
        st["watching"] = self.stages is None or self.stages.looks_at_leaves
        if not st["watching"]:
            # Stage 1 only runs while traffic flows, and flowing traffic means the bridge is
            # down, so say so rather than "unknown". No line: it would be a stale one.
            st.update(position="closed", assumed=True, angle_deg=None, line=None)
        return st

    def state(self) -> dict[str, Any]:
        return {
            "stage": self.stages.state(self.last_t) if self.stages else None,
            "bridge": self._leaf_state(self.bridge),
            "far_leaf": self._leaf_state(self.far_leaf),
            "traffic": {name: tr.state() for name, tr in self.lanes.items()},
            "deck": self.deck.state() if self.deck else None,
            # Blobs in the lane and deck boxes, for the panel to draw (not published).
            "vehicles": [{"box": list(v.box), "moving": v.moving, "lane": name}
                         for name, w in self.watchers.items() if name in self.rois for v in w.vehicles],
            "opening": self.machine.state() if self.machine else None,
            "boat": self.boats.state() if self.boats else {"present": False},
            "detector": {
                "ms_per_frame": round(self.last_ms, 1),
                "yolo": self.yolo is not None,
                "yolo_ms": None if self.yolo is None or self.yolo.last_ms is None else round(self.yolo.last_ms, 1),
                "yolo_error": self.yolo_error,
            },
        }


class DetectorRunner:
    def __init__(self, config, bus, rois: dict | None = None, history=None, ais=None) -> None:
        self.config = config
        self.bus = bus
        self.ais = ais  # an AisSource, or None
        self.pipeline = Pipeline(config, rois, history=history)
        self.listeners: list[Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = self.pipeline.state()
        self.error: str | None = None

    def add_listener(self, fn: Listener) -> None:
        self.listeners.append(fn)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="detectors", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        period = 1.0 / max(self.config.detectors.fps, 0.1)
        seq, next_at = 0, 0.0
        while not self._stop.is_set():
            frame = self.bus.wait_newer(seq, timeout=1.0)
            if frame is None:
                self._tick([])  # still let the trackers time out
                continue
            seq = frame.seq
            now = time.time()
            if now < next_at:
                continue
            next_at = now + period
            try:
                snap = self.ais.snapshot() if self.ais is not None else None
                events = self.pipeline.process(frame.timestamp, frame.image, frame.hires, snap)
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                log.exception("detector failed")
                self._stop.wait(1.0)
                continue
            self._tick(events)

    def _tick(self, events: list[dict[str, Any]]) -> None:
        st = self.pipeline.state()
        st["detector"]["error"] = self.error
        if self.ais is not None:
            st["ais"] = self.ais.snapshot()
        with self._lock:
            self._state = st
        for e in events:
            log.debug("event %s", e)
        for fn in self.listeners:
            try:
                fn(events, st)
            except Exception:
                log.exception("detector listener failed")
