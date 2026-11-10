"""Replay a recorded clip through the detectors, frame by frame, for the panel.

The panel's clip player draws this trace over the video: the boxes, the leaf lines, the
barrier arms at their angle, the cars and boats found, each lane's flow and fill, and the
stage and phase, plus a timeline of every signal. It shows how the detectors read the
clip, which the live log (one entry a second, the stage machine taking turns) can't.

Same ``Pipeline`` as the device and ``wth-edge eval``, at the detector rate, on today's
boxes (plus any box only the clip has), with the stages off so every detector reads every
frame. Nothing it finds is published or recorded: no openings history, no AIS. One clip at a time, in a background
thread at low priority, and the result is saved next to the clip (``<id>.trace``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1


def _r(v, n=3):
    return None if v is None else round(float(v), n)


def frame_record(t: float, st: dict[str, Any]) -> dict[str, Any]:
    """The parts of the pipeline state the player draws, kept small (a 10-minute clip at
    5 fps is 3000 of these)."""
    rec: dict[str, Any] = {"t": round(t, 2)}
    if st.get("opening"):
        rec["phase"] = st["opening"].get("phase")
    for key in ("bridge", "far_leaf"):
        leaf = st.get(key) or {}
        rec[key] = {"pos": leaf.get("position"), "ang": leaf.get("angle_deg"), "line": leaf.get("line")}
    gates = st.get("gates") or {}
    rec["gates"] = {name: {"pos": a.get("position"), "share": a.get("share"), "ang": a.get("angle_deg"),
                           "line": a.get("line")}
                    for name, a in (gates.get("arms") or {}).items()}
    rec["gate_summary"] = gates.get("summary")
    rec["traffic"] = {name: {"lv": lane.get("level"), "flow": _r(lane.get("flow")), "fill": _r(lane.get("fill"))}
                      for name, lane in (st.get("traffic") or {}).items()}
    if st.get("deck") is not None:
        rec["deck"] = {"clear": st["deck"].get("clear")}
    rec["veh"] = [[*(_r(v, 4) for v in c["box"]), 1 if c.get("moving") else 0, c.get("lane")]
                  for c in st.get("vehicles") or []]
    boat = st.get("boat") or {}
    rec["boat"] = {"present": bool(boat.get("present")), "box": boat.get("box"),
                   "conf": _r(boat.get("confidence"), 2), "dir": boat.get("direction")}
    return rec


def trace_clip(config, video: Path, meta: dict[str, Any], rois: dict,
               should_stop: Callable[[], bool] = lambda: False,
               on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    from .detectors import Pipeline
    from .evaluate import _frames

    cfg = copy.deepcopy(config)
    cfg.stages.enabled = False
    pipe = Pipeline(cfg, rois)
    size = tuple(cfg.camera.lores_size)
    duration = float(meta.get("duration_s") or 0) or None
    frames: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    for t, img, hires in _frames(video, cfg.detectors.fps, size, cfg.camera.detector_hires):
        if should_stop():
            break
        events += pipe.process(t, img, hires)
        frames.append(frame_record(t, pipe.state()))
        if on_progress and duration:
            on_progress(min(1.0, t / duration))
    return {
        "version": VERSION,
        "clip": meta.get("id"),
        "made_at": time.time(),
        "fps": cfg.detectors.fps,
        "seconds": frames[-1]["t"] if frames else 0.0,
        "ms_per_frame": round((time.perf_counter() - t0) * 1000 / max(len(frames), 1), 1),
        "rois": {k: vars(v) for k, v in rois.items()},
        "frames": frames,
        "events": [{k: v for k, v in e.items() if not isinstance(v, (dict, list)) or k == "box"} for e in events],
    }


def _lower_priority() -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
    except (AttributeError, OSError):
        pass


class Tracer:
    """Runs one trace at a time in the background and remembers how it went."""

    def __init__(self, config, store) -> None:
        self.config = config
        self.store = store
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None   # {"clip", "progress", "started_at"}
        self._errors: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def status(self, clip_id: str) -> dict[str, Any]:
        with self._lock:
            job = dict(self._job) if self._job else None
            error = self._errors.get(clip_id)
        if job and job["clip"] == clip_id:
            return {"state": "running", "progress": round(job["progress"], 3)}
        if self.store.paths(clip_id)["trace"].is_file():
            return {"state": "done"}
        if error:
            return {"state": "error", "error": error}
        return {"state": "none", "busy_with": job["clip"] if job else None}

    def start(self, clip_id: str) -> dict[str, Any]:
        meta = self.store.read_meta(clip_id)
        video = self.store.paths(clip_id)["video"]
        if meta is None or not video.is_file():
            raise TraceError("no such clip")
        with self._lock:
            if self._job:
                if self._job["clip"] == clip_id:
                    return {"state": "running", "progress": self._job["progress"]}
                raise TraceError(f"already replaying {self._job['clip']}; try again when it's done")
            self._job = {"clip": clip_id, "progress": 0.0, "started_at": time.time()}
            self._errors.pop(clip_id, None)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(clip_id, meta, video), name="trace", daemon=True)
        self._thread.start()
        return {"state": "running", "progress": 0.0}

    def _run(self, clip_id: str, meta: dict[str, Any], video: Path) -> None:
        from . import config as config_mod

        _lower_priority()
        try:
            # Today's boxes (site file plus the panel's edits) over the ones the clip was
            # recorded with: the replay shows how the detectors read it now.
            raw = meta.get("rois") or {}
            rois = {k: config_mod.Roi(**v) for k, v in raw.items()}
            rois.update({k: copy.copy(v) for k, v in self.config.site.rois.items()})

            def progress(p: float) -> None:
                with self._lock:
                    if self._job:
                        self._job["progress"] = p

            trace = trace_clip(self.config, video, meta, rois, self._stop.is_set, progress)
            if self._stop.is_set():
                return
            path = self.store.paths(clip_id)["trace"]
            tmp = path.with_suffix(".trace.tmp")
            tmp.write_text(json.dumps(trace, separators=(",", ":"), default=str))
            os.replace(tmp, path)
            log.info("traced %s: %d frames, %.0f ms a frame", clip_id, len(trace["frames"]), trace["ms_per_frame"])
        except Exception as exc:
            log.exception("trace of %s failed", clip_id)
            with self._lock:
                self._errors[clip_id] = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._job = None

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class TraceError(RuntimeError):
    pass
