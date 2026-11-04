"""Traffic flow per lane, from two cheap measures on the lores frame.

For each box (``northbound``, ``southbound`` and the ``deck``):

* *flow*: the fraction of pixels that changed since the previous detector frame. Cars
  moving through the box make it high; a queue sitting still makes it near zero.
* *fill*: the fraction of pixels that differ from a slowly learned background. Cars make
  it high whether they move or not, so a queue reads as "present".

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

    def update(self, frame: np.ndarray, roi) -> LaneReading | None:
        import cv2

        h, w = frame.shape[:2]
        x1, y1, x2, y2 = roi.to_pixels(w, h)
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        crop = frame[y1:y2, x1:x2]
        gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        s = min(1.0, self.cfg.work_width / gray.shape[1])
        if s < 1:
            gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
        if self._prev is None or self._prev.shape != gray.shape:
            self._prev, self._bg = gray, gray.copy()
            return None
        thr = float(self.cfg.diff_threshold)
        flow = float(np.mean(np.abs(gray - self._prev) > thr))
        fill = float(np.mean(np.abs(gray - self._bg) > thr))
        self._prev = gray
        # The background only learns where nothing is moving, so a queue doesn't fade in.
        a = self.cfg.background_rate
        self._bg += a * (gray - self._bg)
        return LaneReading(round(flow, 4), round(fill, 4))

    def reset(self) -> None:
        self._prev = self._bg = None


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
    """Only "clear or not", for the gates-down test and the release."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._window: deque[tuple[float, LaneReading]] = deque()
        self.fill: float | None = None
        self.flow: float | None = None

    def update(self, t: float, reading: LaneReading | None) -> None:
        if reading is not None:
            self._window.append((t, reading))
        while self._window and t - self._window[0][0] > self.cfg.window_s:
            self._window.popleft()
        if self._window:
            self.fill = round(float(np.mean([r.fill for _, r in self._window])), 4)
            self.flow = round(float(np.mean([r.flow for _, r in self._window])), 4)
        else:
            self.fill = self.flow = None

    @property
    def clear(self) -> bool | None:
        return None if self.fill is None else self.fill < self.cfg.deck_clear_max

    @property
    def moving(self) -> bool | None:
        return None if self.flow is None else self.flow >= self.cfg.flow_min

    def state(self) -> dict[str, Any]:
        return {"clear": self.clear, "moving": self.moving, "fill": self.fill, "flow": self.flow}
