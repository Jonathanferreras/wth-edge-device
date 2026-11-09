"""Traffic flow per lane, from two cheap measures on the lores frame.

For each box (``northbound``, ``southbound`` and the ``deck``):

* *flow*: the fraction of pixels that changed since the previous detector frame. Cars
  moving through the box make it high; a queue sitting still makes it near zero.
* *fill*: the fraction of pixels that differ from a slowly learned background. Cars make
  it high whether they move or not, so a queue reads as "present".

Where the fill mask is set, ``LaneWatcher`` also keeps the separate blobs as boxes (frame
fractions) so the panel can draw the cars it sees. They are blobs of "not road", not a
car classifier: two cars bumper to bumper make one box.

``LaneTracker`` averages both over a short window and names a level the way the web app
does: ``light``, ``moderate`` or ``heavy`` from the fill while traffic moves, ``standstill``
when the lane is filled but nothing moves, ``unknown`` with no frames. The deck only
needs "clear or not".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

LEVELS = ("light", "moderate", "heavy", "standstill", "unknown")


Box = tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions of the frame


@dataclass
class Vehicle:
    box: Box
    moving: bool


@dataclass
class LaneReading:
    flow: float   # 0-1, fraction of the box that changed since the last frame
    fill: float   # 0-1, fraction of the box that differs from the background


class LaneWatcher:
    """Measures one box. Keeps the previous frame and a running background."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._prev: np.ndarray | None = None
        self._bg: np.ndarray | None = None
        self.vehicles: list[Vehicle] = []

    def update(self, frame: np.ndarray, roi) -> LaneReading | None:
        import cv2

        h, w = frame.shape[:2]
        x1, y1, x2, y2 = roi.to_pixels(w, h)
        if x2 - x1 < 4 or y2 - y1 < 4:
            self.vehicles = []
            return None
        crop = frame[y1:y2, x1:x2]
        gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        s = min(1.0, self.cfg.work_width / gray.shape[1])
        if s < 1:
            gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
        if self._prev is None or self._prev.shape != gray.shape:
            self._prev, self._bg = gray, gray.copy()
            self.vehicles = []
            return None
        thr = float(self.cfg.diff_threshold)
        moved = np.abs(gray - self._prev) > thr
        present = np.abs(gray - self._bg) > thr
        flow = float(np.mean(moved))
        fill = float(np.mean(present))
        self._prev = gray
        self.vehicles = self._blobs(present | moved, moved, (x1, y1, x2, y2), (w, h))
        # The background only learns where nothing is moving, so a queue doesn't fade in.
        a = self.cfg.background_rate
        self._bg += a * (gray - self._bg)
        return LaneReading(round(flow, 4), round(fill, 4))

    def _blobs(self, mask: np.ndarray, moved: np.ndarray, px: tuple, size: tuple) -> list[Vehicle]:
        """Connected blobs of the mask, as frame-fraction boxes, biggest first."""
        import cv2

        min_area = self.cfg.vehicle_min_area * mask.size
        if not self.cfg.vehicle_boxes or mask.sum() < min_area:
            return []
        m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        x1, y1, x2, y2 = px
        W, H = size
        sx, sy = (x2 - x1) / m.shape[1], (y2 - y1) / m.shape[0]
        out = []
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            if area < min_area:
                continue
            box = ((x1 + bx * sx) / W, (y1 + by * sy) / H, (x1 + (bx + bw) * sx) / W, (y1 + (by + bh) * sy) / H)
            still = float(np.mean(moved[by:by + bh, bx:bx + bw])) < self.cfg.flow_min * 4
            out.append((area, Vehicle(tuple(round(v, 4) for v in box), not still)))
        out.sort(key=lambda a: -a[0])
        return [v for _, v in out[: self.cfg.vehicle_max]]

    def reset(self) -> None:
        self._prev = self._bg = None
        self.vehicles = []


class LaneTracker:
    """Readings in, a traffic level out, with a hold time against flicker."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._window: deque[tuple[float, LaneReading]] = deque()
        self.level = "unknown"
        self.since: float | None = None
        self._candidate: str | None = None
        self._candidate_since = 0.0
        self._still_since: float | None = None
        self.flow: float | None = None
        self.fill: float | None = None
        self.last_at: float | None = None

    def _target(self, t: float) -> str:
        cfg = self.cfg
        if self.fill is None or self.flow is None:
            return "unknown"
        present = self.fill >= cfg.present_min
        moving = self.flow >= cfg.flow_min
        if present and not moving:
            if self._still_since is None:
                self._still_since = t
            if t - self._still_since >= cfg.standstill_s:
                return "standstill"
            return self.level if self.level in ("light", "moderate", "heavy") else "moderate"
        self._still_since = None
        if self.fill >= cfg.heavy_min:
            return "heavy"
        if self.fill >= cfg.moderate_min:
            return "moderate"
        return "light"

    def update(self, t: float, reading: LaneReading | None) -> str | None:
        """Feed one measurement (None = box unreadable). Returns the level when it changes."""
        cfg = self.cfg
        if reading is not None:
            self.last_at = t
            self._window.append((t, reading))
        while self._window and t - self._window[0][0] > cfg.window_s:
            self._window.popleft()
        if self._window:
            self.flow = round(float(np.mean([r.flow for _, r in self._window])), 4)
            self.fill = round(float(np.mean([r.fill for _, r in self._window])), 4)
        else:
            self.flow = self.fill = None
        target = self._target(t)
        if target == self.level:
            self._candidate = None
            return None
        if target != self._candidate:
            self._candidate, self._candidate_since = target, t
        if target == "unknown" or t - self._candidate_since >= cfg.hold_s or self.level == "unknown":
            self.level, self.since, self._candidate = target, t, None
            return target
        return None

    @property
    def moving(self) -> bool | None:
        return None if self.flow is None else self.flow >= self.cfg.flow_min

    @property
    def confidence(self) -> float:
        if self.level == "unknown" or not self._window:
            return 0.0
        # More frames in the window and a clearer margin from the thresholds both help.
        n = min(1.0, len(self._window) / 10)
        return round(0.5 + 0.4 * n, 2)

    def state(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "confidence": self.confidence,
            "flow": self.flow,
            "fill": self.fill,
            "moving": self.moving,
            "since": self.since,
        }


class DeckTracker:
    """Only "clear or not", for the gates-down test and the release.

    Clear means one of two things: both lanes are at a standstill (the queue is held at the
    gates), or genuinely no car has crossed the deck for ``deck_clear_s``. A window average
    of the fill is not enough on its own: a few cars driving through barely move it, so the
    deck read "clear" with cars passing. Any frame with a moving car blob, or with more
    change than ``flow_min``, counts as a car passing.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._window: deque[tuple[float, LaneReading]] = deque()
        self.fill: float | None = None
        self.flow: float | None = None
        self.last_car_at: float | None = None
        self.lanes_standstill = False
        self._t: float | None = None

    def update(self, t: float, reading: LaneReading | None, vehicles: list[Vehicle] | None = None,
               lanes_standstill: bool = False) -> None:
        self._t = t
        self.lanes_standstill = lanes_standstill
        if reading is not None:
            self._window.append((t, reading))
            if reading.flow >= self.cfg.flow_min or any(v.moving for v in vehicles or ()):
                self.last_car_at = t
        while self._window and t - self._window[0][0] > self.cfg.window_s:
            self._window.popleft()
        if self._window:
            self.fill = round(float(np.mean([r.fill for _, r in self._window])), 4)
            self.flow = round(float(np.mean([r.flow for _, r in self._window])), 4)
        else:
            self.fill = self.flow = None

    @property
    def quiet_for(self) -> float | None:
        """Seconds since a car last crossed the deck (since the first frame if none has)."""
        if self._t is None or not self._window:
            return None
        since = self.last_car_at if self.last_car_at is not None else self._window[0][0]
        return self._t - since

    @property
    def clear(self) -> bool | None:
        if self.fill is None:
            return None
        if self.lanes_standstill:
            return True
        quiet = self.quiet_for
        return self.fill < self.cfg.deck_clear_max and quiet is not None and quiet >= self.cfg.deck_clear_s

    @property
    def moving(self) -> bool | None:
        return None if self.flow is None else self.flow >= self.cfg.flow_min

    def state(self) -> dict[str, Any]:
        quiet = self.quiet_for
        return {"clear": self.clear, "moving": self.moving, "fill": self.fill, "flow": self.flow,
                "quiet_s": None if quiet is None else round(quiet, 1), "lanes_standstill": self.lanes_standstill}
