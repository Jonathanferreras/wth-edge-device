"""The barrier arms: up (traffic may pass) or down (an opening is coming).

Each ``gate_*`` box is drawn around one raised arm. The arms are yellow-orange, so
``measure_gate`` counts the share of the box's rows that hold arm-coloured pixels: nearly
all of them with the arm up, only the housing at the bottom with it down. Straight edges
were tried first and fail: with the arm gone, the trees behind the box give long vertical
lines that read as an arm.

For the overlay it also finds the arm's angle: from the arm's base (the bottom middle of
the box), the direction with the most arm pixels along an arm's length. ``GateTracker``
smooths the readings into up / down / unknown with a hold time, and ``summary`` combines
the arms, weighting each by how steady its readings are.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

POSITIONS = ("up", "down", "unknown")


@dataclass
class GateReading:
    share: float                 # share of the box's rows with arm pixels
    dark: bool                   # too dark to see the colour
    angle_deg: float | None = None  # the arm's direction from its base, 0-180 (90 = up)
    pivot: tuple[float, float] | None = None  # the base, in frame fractions
    length: float = 0.0          # the arm's length (the box height), in frame fractions of the height
    aspect: float = 16 / 9       # frame width over height

    def position(self, cfg) -> str | None:
        if self.dark:
            return None
        if self.share >= cfg.up_min:
            return "up"
        if self.share <= cfg.down_max:
            return "down"
        return None


def arm_mask(rgb: np.ndarray, cfg) -> np.ndarray:
    import cv2

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    lo = np.array([cfg.hue_min, cfg.sat_min, cfg.val_min], np.uint8)
    hi = np.array([cfg.hue_max, 255, 255], np.uint8)
    return cv2.inRange(hsv, lo, hi) > 0


def measure_gate(frame: np.ndarray, roi, cfg) -> GateReading | None:
    """``frame``: RGB. None when the box is off the frame or the frame has no colour."""
    if frame.ndim != 3:
        return None
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi.to_pixels(w, h)
    if x2 - x1 < 2 or y2 - y1 < 4:
        return None
    crop = frame[y1:y2, x1:x2]
    dark = float(crop.mean()) < cfg.dark_below
    rows = arm_mask(crop, cfg).any(axis=1)
    reading = GateReading(float(rows.mean()), dark)
    if not dark:
        _swing(frame, (x1 + x2) / 2, y2, y2 - y1, cfg, reading)
    return reading


def _swing(frame: np.ndarray, px: float, py: float, length: float, cfg, reading: GateReading) -> None:
    """The arm's angle: sample points along each direction from the base, from a quarter of
    the arm's length to its end, and take the direction with the most arm pixels."""
    h, w = frame.shape[:2]
    reach = math.ceil(length)
    bx1, by1 = max(0, int(px) - reach), max(0, int(py) - reach)
    bx2, by2 = min(w, int(px) + reach + 1), min(h, int(py) + 2)
    if bx2 - bx1 < 2 or by2 - by1 < 2:
        return
    mask = arm_mask(frame[by1:by2, bx1:bx2], cfg)
    steps = np.linspace(0.25, 1.0, max(6, int(length * 0.75)))
    best, best_share = None, 0.0
    for deg in np.arange(0.0, 180.0 + 1e-6, cfg.swing_step_deg):
        rad = math.radians(deg)
        xs = np.round(px + math.cos(rad) * length * steps).astype(int) - bx1
        ys = np.round(py - math.sin(rad) * length * steps).astype(int) - by1
        ok = (xs >= 0) & (xs < mask.shape[1]) & (ys >= 0) & (ys < mask.shape[0])
        if ok.sum() < len(steps) // 2:
            continue
        share = float(mask[ys[ok], xs[ok]].mean())
        # Prefer the steeper direction on a tie: the post and housing are below the base.
        if share > best_share or (share == best_share and best is not None and abs(deg - 90) < abs(best - 90)):
            best, best_share = float(deg), share
    reading.pivot = (px / w, py / h)
    reading.length = length / h
    reading.aspect = w / h
    if best is not None and best_share >= cfg.swing_min_share:
        reading.angle_deg = best


class GateTracker:
    """Majority of the up/down readings over ``smooth_s``, held ``hold_s`` before it changes."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._seen: deque[tuple[float, str | None]] = deque()
        self._angles: deque[tuple[float, float]] = deque()
        self._last_found: float | None = None
        self.position = "unknown"
        self.since: float | None = None
        self._candidate: str | None = None
        self._candidate_since = 0.0
        self.share: float | None = None
        self.dark = False
        self.pivot: tuple[float, float] | None = None
        self.length = 0.0
        self.aspect = 16 / 9

    def update(self, t: float, reading: GateReading | None) -> str | None:
        """Feed one reading. Returns the new position when it changes."""
        cfg = self.cfg
        pos = reading.position(cfg) if reading is not None else None
        self._seen.append((t, pos))
        while self._seen and t - self._seen[0][0] > cfg.smooth_s:
            self._seen.popleft()
        if reading is not None:
            self.share, self.dark = reading.share, reading.dark
            if reading.pivot is not None:
                self.pivot, self.length, self.aspect = reading.pivot, reading.length, reading.aspect
            if reading.angle_deg is not None:
                self._angles.append((t, reading.angle_deg))
        while self._angles and t - self._angles[0][0] > cfg.smooth_s:
            self._angles.popleft()
        if pos is not None:
            self._last_found = t
        if self._last_found is None or t - self._last_found > cfg.lost_s:
            target = "unknown"
        else:
            ups = sum(1 for _, r in self._seen if r == "up")
            downs = sum(1 for _, r in self._seen if r == "down")
            if not ups and not downs:
                return None  # a short gap: keep what we had
            target = "up" if ups >= downs else "down"
        if target == self.position:
            self._candidate = None
            return None
        if target != self._candidate:
            self._candidate, self._candidate_since = target, t
        if target == "unknown" or self.since is None or t - self._candidate_since >= cfg.hold_s:
            self.position, self.since, self._candidate = target, t, None
            return target
        return None

    @property
    def confidence(self) -> float:
        """Share of the recent readings that agree with the position."""
        if self.position == "unknown" or not self._seen:
            return 0.0
        return round(sum(1 for _, r in self._seen if r == self.position) / len(self._seen), 2)

    @property
    def angle(self) -> float | None:
        return statistics.median(a for _, a in self._angles) if self._angles else None

    def line(self) -> list[float] | None:
        """Base to tip of the arm, in frame fractions, at its current angle (for the overlay)."""
        aspect = self.aspect
        angle = self.angle
        if angle is None or self.pivot is None or self.position == "unknown":
            return None
        px, py = self.pivot
        rad = math.radians(angle)
        return [round(px, 4), round(py, 4), round(px + math.cos(rad) * self.length / aspect, 4),
                round(py - math.sin(rad) * self.length, 4)]

    def state(self) -> dict[str, Any]:
        angle = self.angle
        return {"position": self.position, "confidence": self.confidence, "since": self.since,
                "share": None if self.share is None else round(self.share, 2), "dark": self.dark,
                "angle_deg": None if angle is None else round(angle), "line": self.line()}


def summary(arms: list[tuple[str, float]]) -> str | None:
    """All the arms as one signal from (position, confidence) pairs: "down" or "up" by the
    confidence-weighted vote of the readable arms, None when none can be read or the vote
    is even. Weighted, because an arm whose box catches sunlit grass flickers."""
    up = sum(c for p, c in arms if p == "up")
    down = sum(c for p, c in arms if p == "down")
    if up == down:
        return None
    return "down" if down > up else "up"
