"""Detectors: leaf angles, traffic per lane, boat crossings, and the opening state machine.

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


class Pipeline:
    def __init__(self, config, rois: dict | None = None, repo_root=None, history=None) -> None:
        from ..config import REPO_ROOT
        from ..opening import OpeningMachine
        from .boat import BoatTracker, MotionGate, YoloBoat
        from .bridge import BridgeTracker
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
        self.machine = OpeningMachine(config.opening, bcfg, config.site.approach_sides, history) if config.opening.enabled else None
        self.motion = MotionGate(boat) if boat.enabled else None
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
        self.last_ms: float = 0.0

    def process(self, t: float, frame: np.ndarray, hires=None, ais: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """``frame``: the lores RGB frame. ``hires``: an optional ``HiRes`` full-res frame,
        which the bridge (thin edges) and the boat model (small boats) both need. ``ais``:
        the latest ``AisSource.snapshot()``, when there is one."""
        from .bridge import measure_angle

        t0 = time.perf_counter()
        events: list[dict[str, Any]] = []
        rois = self.rois
        for name, which in LEAVES.items():
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
                self.deck.update(t, reading)
            else:
                before = self.lanes[name].level
                changed = self.lanes[name].update(t, reading)
                if changed:
                    events.append({"type": "traffic", "at": t, "direction": name, "level": changed, "previous": before})
        if self.boats is not None and "boat" in rois:
            cfg = self.config.boat_detector
            moving = self.motion.update(frame, rois["boat"]) if self.motion else None
            if moving is not None:
                self._last_motion_at = t
            if self.yolo is not None:
                # Motion or an open crossing runs the model often; otherwise it still looks
                # now and then, because a slow tug fades into the motion background.
                busy = (self._last_motion_at is not None and t - self._last_motion_at < 2.0) or self.boats.current is not None
                hz = cfg.yolo_max_hz if busy else cfg.yolo_idle_hz
                if hz > 0 and t - self._last_yolo >= 1.0 / hz:
                    self._last_yolo = t
                    src = hires.rgb() if hires is not None else frame
                    events += self.boats.update(t, self.yolo.detect(src, rois["boat"]))
                else:
                    events += self.boats.tick(t)
            else:
                events += self.boats.update(t, moving)
        if self.machine is not None:
            events += self.machine.update(t, self.signals(ais))
        self.last_ms = (time.perf_counter() - t0) * 1000
        return events

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

    def state(self) -> dict[str, Any]:
        return {
            "bridge": self.bridge.state() if self.bridge else {"position": "unknown", "confidence": 0.0},
            "far_leaf": self.far_leaf.state() if self.far_leaf else {"position": "unknown", "confidence": 0.0},
            "traffic": {name: tr.state() for name, tr in self.lanes.items()},
            "deck": self.deck.state() if self.deck else None,
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
