"""The barrier arms: up (traffic may pass) or down (an opening is coming).

Each ``gate_*`` box is drawn around one raised arm. ``measure_gate`` looks for the arm as a
long near-vertical edge (Canny and Hough on the contrast-stretched box, so shade and dusk
still show it). A lowered arm lies across the road and leaves its narrow box, so there is
no "down" line to find: ``GateTracker`` calls an arm down when it goes missing from a box
where it is usually seen, and unknown when the box is too dark or the arm is rarely seen
there (some boxes lose the arm at night). ``summary`` combines the arms into one signal.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from .bridge import REF_WIDTH

POSITIONS = ("up", "down", "unknown")


@dataclass
class GateReading:
    up: bool      # the raised arm was found
    dark: bool    # too dark to tell a missing arm from an unlit one
    length: float = 0.0  # the longest vertical line, as a share of the box height


def measure_gate(frame: np.ndarray, roi, cfg) -> GateReading | None:
    import cv2

    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi.to_pixels(w, h)
    if x2 - x1 < 3 or y2 - y1 < 6:
        return None
    crop = frame[y1:y2, x1:x2]
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    dark = float(gray.mean()) < cfg.dark_below
    edges = cv2.Canny(cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX), cfg.canny_low, cfg.canny_high)
    k = w / REF_WIDTH
    bh = y2 - y1
    lines = cv2.HoughLinesP(edges, rho=1, theta=np.pi / 180, threshold=max(3, int(cfg.hough_threshold * k)),
                            minLineLength=max(3, int(bh * cfg.up_min_frac * 0.5)),
                            maxLineGap=max(1, int(cfg.max_line_gap_px * k)))
    best = 0.0
    if lines is not None:
        for lx1, ly1, lx2, ly2 in np.asarray(lines).reshape(-1, 4):
            angle = abs(float(np.degrees(np.arctan2(ly2 - ly1, lx2 - lx1))))
            if min(angle, 180 - angle) >= cfg.up_min_deg:
                best = max(best, float(abs(int(ly2) - int(ly1))) / bh)
    return GateReading(bool(best >= cfg.up_min_frac), dark, round(best, 2))


class GateTracker:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._seconds: deque[list] = deque()  # [whole second, arm seen in it]
        self._last_up: float | None = None
        self.position = "unknown"
        self.since: float | None = None
        self.dark = False

    def trust(self) -> float:
        """Share of the recent seconds the arm was seen in (0 until there is enough history)."""
        if len(self._seconds) < self.cfg.trust_s * 0.5:
            return 0.0
        return sum(1 for _, up in self._seconds if up) / len(self._seconds)

    def update(self, t: float, reading: GateReading | None) -> str | None:
        """Feed one reading. Returns the new position when it changes."""
        cfg = self.cfg
        if reading is not None:
            sec = int(t)
            if self._seconds and self._seconds[-1][0] == sec:
                self._seconds[-1][1] = self._seconds[-1][1] or reading.up
            else:
                self._seconds.append([sec, reading.up])
            while self._seconds and sec - self._seconds[0][0] >= cfg.trust_s:
                self._seconds.popleft()
            self.dark = reading.dark
            if reading.up:
                self._last_up = t
        missing = None if self._last_up is None else t - self._last_up
        if reading is None and (missing is None or missing > cfg.trust_s):
            target = "unknown"
        elif missing is not None and missing <= cfg.up_window_s:
            target = "up"
        elif self.position == "down":
            target = "down"  # stays down until the arm is seen again
        elif self.dark:
            target = "unknown"
        elif missing is not None and missing >= cfg.down_after_s and self.trust() >= cfg.trust_min:
            target = "down"
        elif self.position == "up" and missing is not None and missing < cfg.down_after_s:
            return None  # a short gap: keep "up"
        else:
            target = "unknown"
        if target == self.position:
            return None
        self.position, self.since = target, t
        return target

    def state(self) -> dict[str, Any]:
        return {"position": self.position, "since": self.since, "trust": round(self.trust(), 2), "dark": self.dark}


def summary(positions: list[str]) -> str | None:
    """All the arms as one signal: "down" or "up" when more readable arms say so than the
    other, None when none can be read or they are split evenly."""
    ups = sum(p == "up" for p in positions)
    downs = sum(p == "down" for p in positions)
    if ups == downs:
        return None
    return "down" if downs > ups else "up"
