"""Clip recording with a pre-roll buffer.

Two writers sit behind one interface (``begin(path)`` / ``end()``):

* On a Pi, ``PiCamera`` runs Picamera2's H.264 encoder on the 720p main stream into a
  circular buffer, so a clip starts ``prebuffer_s`` seconds before the trigger.
* Anywhere else (fake or replayed camera), ``BusClipWriter`` keeps recent lores frames
  from the FrameBus in memory and encodes them with PyAV.

``Recorder`` owns the clip lifecycle: ids, the clip.json sidecar, the thumbnail, the
maximum length, and (from phase 3) detector triggers.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import Any

from .clips import SCHEMA, ClipStore, new_clip_id, utc_iso

log = logging.getLogger(__name__)


class RecordingError(RuntimeError):
    pass


class BusClipWriter:
    """Pre-roll recorder fed from the FrameBus (laptops, replays, tests)."""

    def __init__(self, bus, prebuffer_s: float, fps: float) -> None:
        self.bus = bus
        self.prebuffer_s = prebuffer_s
        self.fps = max(fps, 1.0)
        self._buf: deque = deque()
        self._lock = threading.Lock()
        self._container = None
        self._stream = None
        self._t0: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="clip-buffer", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        seq = 0
        while not self._stop.is_set():
            frame = self.bus.wait_newer(seq, timeout=1.0)
            if frame is None:
                continue
            seq = frame.seq
            with self._lock:
                if self._container is not None:
                    try:
                        self._encode(frame)
                    except Exception:
                        # Don't spin on a broken file: stop writing; end() finishes the clip.
                        log.exception("clip encoding failed; the clip ends here")
                        self._abort()
                else:
                    self._buf.append(frame)
                    while self._buf and frame.timestamp - self._buf[0].timestamp > self.prebuffer_s:
                        self._buf.popleft()

    def _encode(self, frame) -> None:
        import av
        import numpy as np

        img = frame.image
        if img.ndim == 2:
            img = np.stack([img] * 3, axis=-1)
        if self._stream is None:
            h, w = img.shape[:2]
            codec = "libx264" if "libx264" in av.codecs_available else "mpeg4"
            self._stream = self._container.add_stream(codec, rate=int(round(self.fps)))
            self._stream.width, self._stream.height = w - w % 2, h - h % 2
            self._stream.pix_fmt = "yuv420p"
            # Millisecond timestamps from the bus, for both the stream and the encoder, so
            # frames arriving faster than the nominal rate don't collapse onto one pts.
            self._stream.time_base = Fraction(1, 1000)
            self._stream.codec_context.time_base = Fraction(1, 1000)
            self._t0 = frame.timestamp
            self._last_pts = -1
        h, w = self._stream.height, self._stream.width
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(img[:h, :w]), format="rgb24")
        pts = int((frame.timestamp - self._t0) * 1000)
        if pts <= self._last_pts:
            return  # duplicate or out-of-order timestamp
        self._last_pts = pts
        vf.pts = pts
        vf.time_base = Fraction(1, 1000)
        for packet in self._stream.encode(vf):
            self._container.mux(packet)

    def begin(self, path: Path) -> float:
        """Start a clip; returns how many seconds of pre-roll it got."""
        import av

        with self._lock:
            self._container = av.open(str(path), mode="w")
            self._stream = None
            span = self._buf[-1].timestamp - self._buf[0].timestamp if len(self._buf) > 1 else 0.0
            try:
                for frame in list(self._buf):
                    self._encode(frame)
            except Exception as exc:
                self._abort()
                raise RecordingError(f"could not start the clip: {exc}") from exc
            self._buf.clear()
            return span

    def _abort(self) -> None:
        try:
            self._container.close()
        except Exception:
            pass
        self._container = None
        self._stream = None

    def end(self) -> None:
        with self._lock:
            if self._container is None:
                return
            if self._stream is not None:
                for packet in self._stream.encode():
                    self._container.mux(packet)
            self._container.close()
            self._container = None
            self._stream = None

    def close(self) -> None:
        self._stop.set()
        self.end()


class Recorder:
    def __init__(self, config, camera, store: ClipStore) -> None:
        self.config = config
        self.rcfg = config.recording
        self.camera = camera
        self.store = store
        self._lock = threading.Lock()
        self._active: dict[str, Any] | None = None
        self._timer: threading.Timer | None = None
        # Set when a clip hit max_duration_s or someone pressed Stop: auto-recording then
        # waits for the scene to go quiet instead of starting a new clip straight away.
        self.hold_auto: str | None = None
        # Set while a self-tuning session owns the recorder: auto-recording keeps out, and
        # the session's own clips (trigger "tune") are never stopped by it.
        self.reserved_by: str | None = None

    @property
    def available(self) -> bool:
        return self.rcfg.enabled and self.camera.clip_writer is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            a = self._active
            return {
                "available": self.available,
                "recording": a is not None,
                "clip_id": a["id"] if a else None,
                "label": a.get("label") if a else None,
                "trigger": a.get("trigger") if a else None,
                "started_at": a["started_at"] if a else None,
                "elapsed_s": round(time.time() - a["started_at"], 1) if a else None,
                "prebuffer_s": self.rcfg.prebuffer_s,
                "max_duration_s": self.rcfg.max_duration_s,
                "labels": list(self.rcfg.labels),
                "reserved_by": self.reserved_by,
            }

    def start(self, trigger: str = "manual", label: str | None = None, note: str = "") -> dict[str, Any]:
        if not self.available:
            raise RecordingError("recording is not available with this camera")
        with self._lock:
            if self._active is not None:
                raise RecordingError("already recording")
            now = time.time()
            clip_id = new_clip_id(self.config.device.id, trigger, now)
            paths = self.store.paths(clip_id)
            paths["video"].parent.mkdir(parents=True, exist_ok=True)
            got = self.camera.clip_writer.begin(paths["video"])
            # The Pi writer can't say; assume the buffer is full, capped by camera uptime.
            prebuffer = got if isinstance(got, (int, float)) else min(
                self.rcfg.prebuffer_s, max(0.0, now - (self.camera.started_at or now)))
            meta = {
                "schema": SCHEMA,
                "id": clip_id,
                "device_id": self.config.device.id,
                "site": self.config.site.name,
                "trigger": trigger,
                "label": label,
                "note": note,
                "keep": False,
                # The clip really starts up to prebuffer_s earlier than the trigger.
                "started_at": now,
                "started_at_utc": utc_iso(now),
                "prebuffer_s": round(prebuffer, 1),
                "ended_at": None,
                "camera": {
                    "sensor": self.camera.sensor,
                    "size": list(self.camera.clip_size()),
                    "framerate": self.config.camera.framerate,
                },
                "rois": {k: vars(v) for k, v in self.config.site.rois.items()},
                # Filled by detectors from phase 3: [{"t": seconds, "boat": {...}, "bridge": {...}}]
                "detections": [],
            }
            self.store.write_meta(meta)
            self._save_thumbnail(paths["thumb"])
            self._active = meta
            self._timer = threading.Timer(self.rcfg.max_duration_s, self._auto_stop, args=(clip_id,))
            self._timer.daemon = True
            self._timer.start()
            log.info("recording %s (%s, label=%s)", clip_id, trigger, label)
            return dict(meta)

    def stop(self, by_user: bool = False) -> dict[str, Any] | None:
        with self._lock:
            meta = self._active
            if meta is None:
                return None
            if by_user and meta["trigger"] != "manual":
                self.hold_auto = meta["id"]
            if self._timer:
                self._timer.cancel()
            try:
                self.camera.clip_writer.end()
            finally:
                self._active = None
            meta["ended_at"] = time.time()
            meta["ended_at_utc"] = utc_iso(meta["ended_at"])
            meta["duration_s"] = round(meta["ended_at"] - meta["started_at"] + meta["prebuffer_s"], 1)
            self.store.write_meta(meta)
            log.info("saved clip %s", meta["id"])
            return dict(meta)

    @property
    def active_trigger(self) -> str | None:
        with self._lock:
            return self._active["trigger"] if self._active else None

    def log_detection(self, entry: dict[str, Any], at: float | None = None) -> None:
        """Append to the active clip's detection log. ``t`` is seconds from the clip's first frame."""
        with self._lock:
            a = self._active
            if a is None:
                return
            now = time.time() if at is None else at
            a["detections"].append({"t": round(now - a["started_at"] + a["prebuffer_s"], 2), **entry})

    def _auto_stop(self, clip_id: str) -> None:
        with self._lock:
            active = self._active and self._active["id"] == clip_id
        if active:
            log.info("clip %s reached max length; stopping", clip_id)
            self.stop()
            self.hold_auto = clip_id

    def _save_thumbnail(self, path: Path) -> None:
        frame = self.camera.bus.latest()
        if frame is None:
            return
        from PIL import Image

        img = Image.fromarray(frame.image).convert("RGB")
        img.thumbnail((320, 180))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        path.write_bytes(buf.getvalue())

    def close(self) -> None:
        self.stop()
