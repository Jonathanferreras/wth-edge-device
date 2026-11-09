"""The reference pass over one recorded chunk: the "what was really there" side.

Two things come out of it:

* **Counts.** YOLO (the boat model file, which is plain COCO YOLOv8) looks at the lanes
  ``ref_fps`` times a second and at the river ``ref_boat_fps`` times a second. Simple
  trackers turn the boxes into cars per direction and boat crossings. This is the slow,
  stronger look the live detector can't afford; it runs in the background at low priority.
* **Moments to review.** Every ``sample_s`` seconds, and wherever the live detector changed
  its reading, a pair of frames ``pair_gap_s`` apart is saved with the boxes drawn on.
  ``review.py`` asks Claude what it sees in them; ``analysis.py`` compares that with what
  the detector logged at the same moment.

Times inside a chunk are seconds from the clip's first frame, the same clock as the clip's
detection log. ``t0`` (wall time of that first frame) stitches chunks into one timeline;
frames a chunk shares with the one before (the recorder's pre-roll) are skipped.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

log = logging.getLogger(__name__)

LANES = ("northbound", "southbound")
BOAT_ROIS = ("boat", "boat_near")
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
BOAT_CLASS = 8
Box = tuple[float, float, float, float]


# ---------------------------------------------------------------- detection log helpers
def readings(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """The per-second entries of a clip's detection log (events left out), by time."""
    return sorted((d for d in meta.get("detections") or [] if "event" not in d and "t" in d), key=lambda d: d["t"])


def live_summary(entry: dict[str, Any] | None) -> dict[str, Any]:
    """The few fields that get compared, from one detection log entry."""
    if not entry:
        return {}
    out: dict[str, Any] = {}
    for lane, v in (entry.get("traffic") or {}).items():
        out[lane] = v.get("level")
    if entry.get("deck"):
        out["deck"] = entry["deck"].get("clear")
    positions = [p for p in ((entry.get("bridge") or {}).get("position"), (entry.get("far_leaf") or {}).get("position")) if p]
    if positions:
        known = [p for p in positions if p != "unknown"]
        out["bridge"] = "unknown" if not known else "closed" if all(p == "closed" for p in known) else "raised"
    out["boat"] = bool((entry.get("boat") or {}).get("present"))
    if entry.get("phase"):
        out["phase"] = entry["phase"]
    return out


def change_times(entries: list[dict[str, Any]]) -> list[float]:
    """Times where the live reading changed (a lane level, the leaves, the deck, a boat, the phase)."""
    out, prev = [], None
    for e in entries:
        cur = live_summary(e)
        if prev is not None and cur != prev:
            out.append(float(e["t"]))
        prev = cur
    return out


def pick_samples(t_from: float, t_to: float, every: float, changes: Iterable[float], cap: int,
                 min_apart: float | None = None) -> list[float]:
    """Moments to review: every reading change first, then a regular grid, never two
    closer than ``min_apart`` (4 s, or less with a finer grid), at most ``cap``."""
    if min_apart is None:
        min_apart = min(4.0, every * 0.9)
    chosen: list[float] = []

    def free(t: float) -> bool:
        return all(abs(t - c) >= min_apart for c in chosen)

    for t in sorted(changes):
        if t_from <= t <= t_to and free(t) and len(chosen) < cap * 0.6:
            chosen.append(round(t, 2))
    t = t_from + every / 2
    grid = []
    while t <= t_to:
        grid.append(t)
        t += every
    # Spread the grid evenly when there's no room for all of it.
    room = max(0, cap - len(chosen))
    if len(grid) > room and room > 0:
        step = len(grid) / room
        grid = [grid[int(i * step)] for i in range(room)]
    elif room == 0:
        grid = []
    for t in grid:
        if free(t):
            chosen.append(round(t, 2))
    return sorted(chosen)[:cap]


# ---------------------------------------------------------------- model
class YoloObjects:
    """YOLOv8 (COCO, ONNX) through OpenCV, returning every box of the classes asked for."""

    def __init__(self, model_path: Path, input_size: int = 640, confidence: float = 0.3) -> None:
        import cv2

        if not model_path.is_file():
            raise FileNotFoundError(f"model not found: {model_path}")
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.size = input_size
        self.confidence = confidence
        self.looks = 0
        self.ms = 0.0

    def detect(self, rgb: np.ndarray, roi, classes: Iterable[int]) -> list[tuple[int, float, Box]]:
        """(class, confidence, frame-fraction box) for each object inside ``roi``."""
        import cv2

        h, w = rgb.shape[:2]
        x1, y1, x2, y2 = roi.to_pixels(w, h)
        crop = rgb[y1:y2, x1:x2]
        if crop.size == 0:
            return []
        size = self.size
        ch, cw = crop.shape[:2]
        scale = min(size / ch, size / cw)
        rw, rh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        px, py = (size - rw) // 2, (size - rh) // 2
        canvas[py:py + rh, px:px + rw] = cv2.resize(crop, (rw, rh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (size, size), swapRB=False, crop=False)
        t0 = time.perf_counter()
        self.net.setInput(blob)
        preds = self.net.forward()[0].T
        self.ms += (time.perf_counter() - t0) * 1000
        self.looks += 1
        classes = list(classes)
        scores = preds[:, 4:]
        cls = np.argmax(scores, axis=1)
        conf = scores[np.arange(len(scores)), cls]
        keep = np.isin(cls, classes) & (conf >= self.confidence)
        if not keep.any():
            return []
        preds, cls, conf = preds[keep], cls[keep], conf[keep]
        boxes = [[float(cx - bw / 2), float(cy - bh / 2), float(bw), float(bh)] for cx, cy, bw, bh in preds[:, :4]]
        idx = cv2.dnn.NMSBoxes(boxes, conf.astype(float).tolist(), self.confidence, 0.45)
        out = []
        for i in np.array(idx).reshape(-1):
            bx, by, bw, bh = boxes[i]
            fx1 = (x1 + (bx - px) / scale) / w
            fy1 = (y1 + (by - py) / scale) / h
            fx2 = (x1 + (bx + bw - px) / scale) / w
            fy2 = (y1 + (by + bh - py) / scale) / h
            out.append((int(cls[i]), round(float(conf[i]), 3),
                        tuple(round(float(np.clip(v, 0, 1)), 4) for v in (fx1, fy1, fx2, fy2))))
        return out


# ---------------------------------------------------------------- tracking
@dataclass
class Track:
    tag: str
    first_t: float
    last_t: float
    box: Box
    first_cx: float
    hits: int = 1
    best: float = 0.0
    kind: str = ""

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2


@dataclass
class Tracker:
    """Greedy nearest-centre matching. Good enough to count, not to follow anyone."""

    max_gap: float
    max_jump: float          # as a multiple of the box width, or of the frame when absolute
    absolute: bool = False
    open: list[Track] = field(default_factory=list)
    done: list[Track] = field(default_factory=list)

    def update(self, t: float, found: list[tuple[str, float, Box, str]]) -> None:
        self.expire(t)
        free = list(self.open)
        for tag, conf, box, kind in sorted(found, key=lambda f: -f[1]):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            reach = self.max_jump if self.absolute else self.max_jump * max(box[2] - box[0], 0.01)
            best, best_d = None, None
            for tr in free:
                if tr.tag != tag:
                    continue
                d = ((tr.cx - cx) ** 2 + (tr.cy - cy) ** 2) ** 0.5
                if d <= reach and (best_d is None or d < best_d):
                    best, best_d = tr, d
            if best is None:
                self.open.append(Track(tag, t, t, box, cx, 1, conf, kind))
            else:
                free.remove(best)
                best.last_t, best.box, best.hits, best.best = t, box, best.hits + 1, max(best.best, conf)

    def expire(self, t: float) -> None:
        still = []
        for tr in self.open:
            (still if t - tr.last_t <= self.max_gap else self.done).append(tr)
        self.open = still

    def finish(self) -> list[Track]:
        self.done += self.open
        self.open = []
        return self.done


def counted(tr: Track) -> bool:
    """Two sightings, or one confident one (a car can cross a short box between looks)."""
    return tr.hits >= 2 or tr.best >= 0.5


def boat_direction(tr: Track, left_is: str, right_is: str, min_travel: float) -> str | None:
    travel = tr.cx - tr.first_cx
    if abs(travel) < min_travel:
        return None
    a, b = (left_is, right_is) if travel > 0 else (right_is, left_is)
    return f"{a[0]}2{b[0]}".lower()


# ---------------------------------------------------------------- frames
def annotate(rgb: np.ndarray, rois: dict, path: Path, quality: int = 80, width: int = 1280) -> None:
    """Save a frame with the detector boxes drawn and named, as the reviewer sees it."""
    from PIL import Image, ImageDraw

    from ..web.app import ROI_COLOURS

    img = Image.fromarray(rgb).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)))
    d = ImageDraw.Draw(img)
    W, H = img.size
    for i, (name, roi) in enumerate(rois.items()):
        x1, y1, x2, y2 = roi.to_pixels(W, H)
        colour = ROI_COLOURS[i % len(ROI_COLOURS)]
        d.rectangle((x1, y1, x2, y2), outline=colour, width=2)
        d.text((x1 + 4, y1 + 2), name, fill=colour)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=quality)


# ---------------------------------------------------------------- the pass
class Reference:
    """One per tuning round; chunks go through ``process`` in recording order so tracks
    carry across chunk boundaries."""

    def __init__(self, config, tcfg, frames_dir: Path, model_path: Path | None = None) -> None:
        self.config = config
        self.tcfg = tcfg
        self.frames_dir = frames_dir
        self.cars = Tracker(max_gap=4.0, max_jump=1.5)
        boat = config.boat_detector
        self.boats = Tracker(max_gap=max(boat.gone_s, 4.0), max_jump=0.2, absolute=True)
        self.last_wall: float | None = None
        self.model: YoloObjects | None = None
        self.model_error: str | None = None
        try:
            from ..config import REPO_ROOT

            path = model_path or Path(boat.model_path)
            if not path.is_absolute():
                path = REPO_ROOT / path
            self.model = YoloObjects(path, boat.input_size, tcfg.ref_confidence)
        except Exception as exc:  # no model file or an OpenCV without DNN
            self.model_error = str(exc)
            log.warning("tune: reference model unavailable (%s); no car or boat counts", exc)

    def process(self, meta: dict[str, Any], video: Path, rois: dict, budget: int) -> dict[str, Any]:
        """Run one chunk. Returns the chunk record (samples with their frame files and the
        live reading at each, plus timing)."""
        from ..evaluate import _frames

        t0_wall = float(meta["started_at"]) - float(meta.get("prebuffer_s") or 0.0)
        entries = readings(meta)
        skip_to = 0.0 if self.last_wall is None else max(0.0, self.last_wall - t0_wall)
        duration = float(meta.get("duration_s") or (entries[-1]["t"] if entries else 0.0))
        gap = self.tcfg.pair_gap_s
        samples = pick_samples(skip_to, max(skip_to, duration - gap), self.tcfg.sample_s,
                               change_times(entries), budget)
        wanted = sorted({(s, "a") for s in samples} | {(round(s + gap, 2), "b") for s in samples})
        files: dict[tuple[float, str], str] = {}
        lanes = {n: rois[n] for n in LANES if n in rois}
        lane_box = _union(list(lanes.values()))
        boat_boxes = [rois[n] for n in BOAT_ROIS if n in rois]
        car_every = 1.0 / max(self.tcfg.ref_fps, 0.01)
        boat_every = 1.0 / max(self.tcfg.ref_boat_fps, 0.01)
        next_car = next_boat = skip_to
        wi, frames, t_last = 0, 0, skip_to
        started = time.perf_counter()
        # 4 frames a second is plenty to place a review frame and feed the model.
        fps = max(self.tcfg.ref_fps, self.tcfg.ref_boat_fps, 4.0)
        for t, rgb, _ in _frames(video, fps, None, False):
            if t < skip_to:
                continue
            frames += 1
            t_last = t
            while wi < len(wanted) and t >= wanted[wi][0]:
                s, which = wanted[wi]
                name = f"{meta['id']}_{s:07.2f}{which}.jpg"
                annotate(rgb, rois, self.frames_dir / name)
                files[(s, which)] = name
                wi += 1
            if self.model is None:
                continue
            if lane_box is not None and t >= next_car:
                next_car = t + car_every
                found = []
                for cls, conf, box in self.model.detect(rgb, lane_box, VEHICLE_CLASSES):
                    lane = _lane_of(box, lanes)
                    if lane:
                        found.append((lane, conf, box, VEHICLE_CLASSES[cls]))
                self.cars.update(t0_wall + t, found)
            if boat_boxes and t >= next_boat:
                next_boat = t + boat_every
                found = []
                for roi in boat_boxes:
                    found += [("boat", conf, box, "boat") for _, conf, box in self.model.detect(rgb, roi, [BOAT_CLASS])]
                self.boats.update(t0_wall + t, found)
        self.last_wall = t0_wall + t_last
        out_samples = []
        for s in samples:
            a, b = files.get((s, "a")), files.get((round(s + gap, 2), "b"))
            if a and b:
                out_samples.append({"t": s, "wall": round(t0_wall + s, 2), "a": a, "b": b})
        return {
            "clip_id": meta["id"],
            "t0": round(t0_wall, 2),
            "from_t": round(skip_to, 2),
            "to_t": round(t_last, 2),
            "frames": frames,
            "seconds": round(time.perf_counter() - started, 1),
            "samples": out_samples,
            "rois": {k: vars(v) for k, v in rois.items()},
        }

    def counts(self) -> dict[str, Any]:
        """Totals for the round (call once, after the last chunk)."""
        cfg, site = self.config.boat_detector, self.config.site
        cars = {lane: 0 for lane in LANES}
        kinds: dict[str, int] = {}
        for tr in self.cars.finish():
            if counted(tr):
                cars[tr.tag] = cars.get(tr.tag, 0) + 1
                kinds[tr.kind] = kinds.get(tr.kind, 0) + 1
        boats = []
        for tr in self.boats.finish():
            if tr.hits >= 2:
                boats.append({"at": round(tr.first_t, 1), "until": round(tr.last_t, 1),
                              "direction": boat_direction(tr, site.left_is, site.right_is, cfg.min_travel),
                              "confidence": round(tr.best, 2)})
        boats.sort(key=lambda b: b["at"])
        m = self.model
        return {"cars": cars, "vehicle_kinds": kinds, "boats": boats, "model": m is not None,
                "model_error": self.model_error,
                "ms_per_look": round(m.ms / m.looks, 1) if m and m.looks else None}


def _union(rois: list):
    if not rois:
        return None
    from ..config import Roi

    x1 = min(r.x for r in rois)
    y1 = min(r.y for r in rois)
    x2 = max(r.x + r.w for r in rois)
    y2 = max(r.y + r.h for r in rois)
    return Roi(x1, y1, x2 - x1, y2 - y1)


def _lane_of(box: Box, lanes: dict) -> str | None:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for name, r in lanes.items():
        if r.x <= cx <= r.x + r.w and r.y <= cy <= r.y + r.h:
            return name
    return None
