"""Bridge position from the angle of the bascule leaf.

``measure_angle`` is the old ``wth-inference-training/src/bridge.py`` logic: Canny edges
in the bridge ROI, probabilistic Hough lines, and the longest line's angle from
horizontal (0 = deck flat, 90 = vertical). ``BridgeTracker`` turns a noisy stream of
angles into the web app's positions: closed, opening, open, closing or unknown.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

# The old detector's pixel parameters were tuned on 1280x720 frames.
REF_WIDTH = 1280


@dataclass
class AngleReading:
    angle_deg: float
    # The chosen line in frame fractions (x1, y1, x2, y2), for the overlay.
    line: tuple[float, float, float, float]
    length_px: float


def measure_angle(frame: np.ndarray, roi, cfg) -> AngleReading | None:
    import cv2

    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi.to_pixels(w, h)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    crop = frame[y1:y2, x1:x2]
    # Scale the pixel parameters rather than the image: upscaling blurs the edges away.
    k = w / REF_WIDTH
    gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, cfg.canny_low, cfg.canny_high)
    lines = cv2.HoughLinesP(edges, rho=1, theta=np.pi / 180, threshold=max(8, int(cfg.hough_threshold * k)),
                            minLineLength=max(8, int(cfg.min_line_px * k)), maxLineGap=max(3, int(10 * k)))
    if lines is None:
        return None
    best = None
    for lx1, ly1, lx2, ly2 in np.asarray(lines).reshape(-1, 4):  # (N,1,4) in OpenCV 4, (N,4) in 5
        length = float(np.hypot(lx2 - lx1, ly2 - ly1))
        angle = abs(float(np.degrees(np.arctan2(ly2 - ly1, lx2 - lx1))))
        if angle > 90:
            angle = 180 - angle
        if angle > cfg.max_line_deg:
            continue
        if best is None or length > best[0]:
            best = (length, angle, (lx1, ly1, lx2, ly2))
    if best is None:
        return None
    length, angle, (lx1, ly1, lx2, ly2) = best
    to_frame = lambda px, py: ((x1 + px) / w, (y1 + py) / h)  # noqa: E731
    a, b = to_frame(lx1, ly1), to_frame(lx2, ly2)
    return AngleReading(angle, (a[0], a[1], b[0], b[1]), length / k)


class BridgeTracker:
    """Smooths angles and decides the position, with a hold time against flicker."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._angles: deque[tuple[float, float]] = deque()   # (t, angle)
        self._seen: deque[tuple[float, bool]] = deque()       # (t, found a line)
        self.position = "unknown"
        self.since: float | None = None
        self._candidate: str | None = None
        self._candidate_since = 0.0
        self._last_found: float | None = None
        self.angle: float | None = None
        self.line: tuple[float, float, float, float] | None = None

    def _zone(self, angle: float) -> str:
        if angle <= self.cfg.closed_max_deg:
            return "closed"
        if angle >= self.cfg.open_min_deg:
            return "open"
        return "moving"

    def _rate(self) -> float:
        """Degrees per second over the smoothing window (least squares slope)."""
        if len(self._angles) < 3:
            return 0.0
        ts = np.array([t for t, _ in self._angles])
        ys = np.array([a for _, a in self._angles])
        if np.ptp(ts) < 0.3:
            return 0.0
        return float(np.polyfit(ts - ts[0], ys, 1)[0])

    def update(self, t: float, reading: AngleReading | None) -> str | None:
        """Feed one measurement. Returns the new position when it changes."""
        cfg = self.cfg
        self._seen.append((t, reading is not None))
        while self._seen and t - self._seen[0][0] > cfg.smooth_s:
            self._seen.popleft()
        if reading is not None:
            self._last_found = t
            self._angles.append((t, reading.angle_deg))
            self.line = reading.line
        while self._angles and t - self._angles[0][0] > cfg.smooth_s:
            self._angles.popleft()

        if self._last_found is None or t - self._last_found > cfg.lost_s:
            target = "unknown"
            self.angle, self.line = None, None
        elif not self._angles:
            return None  # a short gap: keep what we had
        else:
            self.angle = statistics.median(a for _, a in self._angles)
            zone = self._zone(self.angle)
            if zone != "moving":
                target = zone
            else:
                rate = self._rate()
                if rate >= cfg.min_rate_deg_s:
                    target = "opening"
                elif rate <= -cfg.min_rate_deg_s:
                    target = "closing"
                elif self.position in ("opening", "closing"):
                    target = self.position
                elif self.position == "closed":
                    target = "opening"
                elif self.position == "open":
                    target = "closing"
                else:
                    target = "unknown"

        if target == self.position:
            self._candidate = None
            return None
        if target != self._candidate:
            self._candidate, self._candidate_since = target, t
        # Losing the line is not worth waiting on; everything else must hold.
        if target == "unknown" or t - self._candidate_since >= cfg.hold_s or self.position == "unknown" and self.since is None:
            self.position, self.since, self._candidate = target, t, None
            return target
        return None

    @property
    def rate(self) -> float:
        return round(self._rate(), 2)

    @property
    def confidence(self) -> float:
        if self.position == "unknown" or not self._seen:
            return 0.0
        return round(sum(1 for _, ok in self._seen if ok) / len(self._seen), 2)

    def state(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "confidence": self.confidence,
            "angle_deg": None if self.angle is None else round(self.angle, 1),
            "since": self.since,
            "line": None if self.line is None else [round(v, 4) for v in self.line],
        }
