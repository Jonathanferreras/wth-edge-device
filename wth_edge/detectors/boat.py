"""Boat crossings in the boat ROI.

Three parts:

* ``MotionGate``: background subtraction on the ROI. Cheap, runs on every detector frame,
  and wakes the model only when something moves.
* ``YoloBoat``: YOLOv8 (ONNX) through OpenCV's DNN module, as the Pi 3 code did. Keeps
  the best ``boat`` box.
* ``BoatTracker``: turns sightings into crossings: start, direction (once the boat has
  travelled far enough), and end after ``gone_s`` without a sighting.

All boxes are fractions of the full frame.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 as frame fractions


@dataclass
class Sighting:
    box: Box
    confidence: float
    source: str  # "yolo" | "motion"

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2


def _crop(frame: np.ndarray, roi) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi.to_pixels(w, h)
    return frame[y1:y2, x1:x2], (x1, y1, x2, y2)


class MotionGate:
    WORK_WIDTH = 320

    def __init__(self, cfg) -> None:
        import cv2

        self.cfg = cfg
        self.sub = cv2.createBackgroundSubtractorMOG2(history=150, varThreshold=32, detectShadows=False)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    def update(self, frame: np.ndarray, roi) -> Sighting | None:
        import cv2

        crop, (x1, y1, x2, y2) = _crop(frame, roi)
        if crop.size == 0:
            return None
        h, w = frame.shape[:2]
        s = min(1.0, self.WORK_WIDTH / crop.shape[1])
        small = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else crop
        gray = small if small.ndim == 2 else cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        mask = self.sub.apply(gray)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.dilate(mask, self.kernel, iterations=2)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        if n <= 1:
            return None
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        area = stats[i, cv2.CC_STAT_AREA]
        if area < self.cfg.motion_min_area * mask.size:
            return None
        bx, by, bw, bh = (int(v) for v in stats[i, :4])
        box = ((x1 + bx / s) / w, (y1 + by / s) / h, (x1 + (bx + bw) / s) / w, (y1 + (by + bh) / s) / h)
        return Sighting(box, round(min(1.0, area / mask.size * 10), 2), "motion")


class YoloBoat:
    def __init__(self, cfg, repo_root: Path) -> None:
        import cv2

        self.cfg = cfg
        path = Path(cfg.model_path)
        if not path.is_absolute():
            path = repo_root / path
        if not path.is_file():
            raise FileNotFoundError(f"boat model not found: {path}")
        self.net = cv2.dnn.readNetFromONNX(str(path))
        self.path = path
        self.last_ms: float | None = None

    def detect(self, frame: np.ndarray, roi) -> Sighting | None:
        import cv2

        crop, (x1, y1, _, _) = _crop(frame, roi)
        if crop.size == 0:
            return None
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2RGB)
        size = self.cfg.input_size
        ch, cw = crop.shape[:2]
        scale = min(size / ch, size / cw)
        rw, rh = int(round(cw * scale)), int(round(ch * scale))
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        px, py = (size - rw) // 2, (size - rh) // 2
        canvas[py:py + rh, px:px + rw] = cv2.resize(crop, (rw, rh), interpolation=cv2.INTER_LINEAR)
        # Frames are RGB already, so no channel swap.
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (size, size), swapRB=False, crop=False)
        t0 = time.perf_counter()
        self.net.setInput(blob)
        preds = self.net.forward()[0].T  # (N, 4 + classes)
        self.last_ms = (time.perf_counter() - t0) * 1000
        scores = preds[:, 4 + self.cfg.class_id]
        keep = scores >= self.cfg.confidence
        if not keep.any():
            return None
        preds, scores = preds[keep], scores[keep]
        # Only the boat class: also drop boxes where another class scores higher.
        best_cls = np.argmax(preds[:, 4:], axis=1)
        mine = best_cls == self.cfg.class_id
        if not mine.any():
            return None
        preds, scores = preds[mine], scores[mine]
        j = int(np.argmax(scores))
        cx, cy, bw, bh = preds[j, :4]
        h, w = frame.shape[:2]
        bx1 = (cx - bw / 2 - px) / scale
        by1 = (cy - bh / 2 - py) / scale
        bx2 = (cx + bw / 2 - px) / scale
        by2 = (cy + bh / 2 - py) / scale
        box = (
            float(np.clip((x1 + bx1) / w, 0, 1)), float(np.clip((y1 + by1) / h, 0, 1)),
            float(np.clip((x1 + bx2) / w, 0, 1)), float(np.clip((y1 + by2) / h, 0, 1)),
        )
        return Sighting(box, round(float(scores[j]), 3), "yolo")


@dataclass
class Crossing:
    started_at: float
    first_cx: float
    last_seen: float
    hits: int = 1
    best_confidence: float = 0.0
    direction: str | None = None   # "w2e" / "e2w" style, from the site's left/right
    box: Box | None = None
    announced: bool = False
    path: list[tuple[float, float]] = field(default_factory=list)


class BoatTracker:
    """Sightings in, crossing events out."""

    def __init__(self, cfg, left_is: str = "WEST", right_is: str = "EAST", require_travel: bool = False) -> None:
        self.cfg = cfg
        # Motion-only mode: ripples move too, so a crossing only counts once it has travelled.
        self.require_travel = require_travel
        self.left, self.right = left_is.upper(), right_is.upper()
        self.current: Crossing | None = None

    def _code(self, rightward: bool) -> str:
        a, b = (self.left, self.right) if rightward else (self.right, self.left)
        return f"{a[0]}2{b[0]}".lower()

    def update(self, t: float, sighting: Sighting | None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        c = self.current
        if sighting is not None:
            if c is None:
                c = self.current = Crossing(t, sighting.cx, t)
            else:
                c.hits += 1
                c.last_seen = t
            c.box = sighting.box
            c.best_confidence = max(c.best_confidence, sighting.confidence)
            c.path.append((round(t - c.started_at, 2), round(sighting.cx, 4)))
            if c.direction is None and abs(sighting.cx - c.first_cx) >= self.cfg.min_travel:
                c.direction = self._code(sighting.cx > c.first_cx)
                if c.announced:
                    events.append(self._event("boat_direction", t))
            if not c.announced and c.hits >= self.cfg.min_hits and (c.direction or not self.require_travel):
                c.announced = True
                events.append(self._event("boat_started", t))
        elif c is not None:
            c.box = None
            if t - c.last_seen > self.cfg.gone_s:
                if c.announced:
                    events.append(self._event("boat_ended", t))
                self.current = None
        return events

    def tick(self, t: float) -> list[dict[str, Any]]:
        """No new look at the frame: only end a crossing that has gone quiet."""
        c = self.current
        if c is not None and t - c.last_seen > self.cfg.gone_s:
            return self.update(t, None)
        return []

    def _event(self, kind: str, t: float) -> dict[str, Any]:
        c = self.current
        assert c is not None
        e = {"type": kind, "at": t, "direction": c.direction, "confidence": round(c.best_confidence, 3),
             "started_at": c.started_at}
        if kind == "boat_ended":
            e["duration_s"] = round(c.last_seen - c.started_at, 1)
            e["hits"] = c.hits
        return e

    @property
    def present(self) -> bool:
        return self.current is not None and self.current.announced

    def state(self) -> dict[str, Any]:
        c = self.current
        return {
            "present": self.present,
            "direction": c.direction if c else None,
            "confidence": round(c.best_confidence, 3) if c and c.announced else 0.0,
            "since": c.started_at if self.present else None,
            "box": [round(v, 4) for v in c.box] if c and c.box and c.announced else None,
        }
