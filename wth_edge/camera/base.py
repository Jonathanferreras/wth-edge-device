"""Camera interface and the in-memory frame bus every consumer reads from.

One camera driver owns the hardware and pushes frames onto a ``FrameBus``. The web
preview, and later the detectors and recorder, read the latest frame from the bus, so
nothing ever opens the camera twice or decodes a stream the Pi just encoded.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)


class HiRes:
    """A full-resolution frame for detectors, converted only when asked.

    ``kind`` is "yuv420" (Picamera2's main stream: Y plane, then quarter-size U and V)
    or "rgb". Greyscale from YUV is free (the Y plane); RGB costs a conversion, so the
    boat model asks for it only when it runs.
    """

    def __init__(self, data: np.ndarray, kind: str, size: tuple[int, int]) -> None:
        self.data, self.kind, self.size = data, kind, size
        self._grey: np.ndarray | None = None
        self._rgb: np.ndarray | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return self.size[1], self.size[0]

    def grey(self) -> np.ndarray:
        if self._grey is None:
            w, h = self.size
            if self.kind == "yuv420":
                self._grey = self.data[:h, :w]
            else:
                import cv2

                self._grey = cv2.cvtColor(self.data, cv2.COLOR_RGB2GRAY)
        return self._grey

    def rgb(self) -> np.ndarray:
        if self._rgb is None:
            if self.kind == "rgb":
                self._rgb = self.data
            else:
                import cv2

                w, h = self.size
                if self.data.shape[1] == w:
                    self._rgb = cv2.cvtColor(self.data, cv2.COLOR_YUV2RGB_I420)
                else:  # padded rows: fall back to grey
                    self._rgb = cv2.cvtColor(self.grey(), cv2.COLOR_GRAY2RGB)
        return self._rgb


@dataclass
class Frame:
    seq: int
    timestamp: float
    # HxWx3 uint8 RGB, or HxW uint8 greyscale when the stream only offers YUV.
    image: np.ndarray
    # Optional full-resolution frame for detectors.
    hires: HiRes | None = None


@dataclass
class CameraHealth:
    ok: bool = False
    fps: float = 0.0
    frame_age_s: float | None = None
    frames: int = 0
    error: str | None = None
    sensor: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fps": round(self.fps, 1),
            "frame_age_s": None if self.frame_age_s is None else round(self.frame_age_s, 2),
            "frames": self.frames,
            "error": self.error,
            "sensor": self.sensor,
        }


@dataclass
class FrameBus:
    _cond: threading.Condition = field(default_factory=threading.Condition)
    _latest: Frame | None = None
    _seq: int = 0
    _times: deque = field(default_factory=lambda: deque(maxlen=60))

    def publish(self, image: np.ndarray, timestamp: float | None = None, hires: HiRes | None = None) -> Frame:
        with self._cond:
            self._seq += 1
            ts = timestamp if timestamp is not None else time.time()
            self._latest = Frame(self._seq, ts, image, hires)
            self._times.append(ts)
            self._cond.notify_all()
            return self._latest

    def latest(self) -> Frame | None:
        with self._cond:
            return self._latest

    def wait_newer(self, seq: int, timeout: float) -> Frame | None:
        """Block until a frame newer than ``seq`` arrives, or return None on timeout."""
        with self._cond:
            if self._latest is not None and self._latest.seq > seq:
                return self._latest
            self._cond.wait(timeout)
            if self._latest is not None and self._latest.seq > seq:
                return self._latest
            return None

    def fps(self) -> float:
        with self._cond:
            if len(self._times) < 2:
                return 0.0
            span = self._times[-1] - self._times[0]
            return (len(self._times) - 1) / span if span > 0 else 0.0


class Camera:
    """Base class: a driver runs ``_capture_loop`` in a thread and publishes frames."""

    name = "base"
    # Give up after this many failures in a row without a single frame (None = never).
    # The process then exits and systemd restarts it with a fresh camera stack.
    max_consecutive_failures: int | None = None

    def __init__(self, cfg, bus: FrameBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self.sensor: str | None = None
        self.fatal = False
        # Set by setup_recording(): an object with begin(path) / end().
        self.clip_writer = None
        self._rcfg = None
        self.started_at: float | None = None

    def setup_recording(self, rcfg) -> None:
        """Prepare the pre-roll buffer. Default: encode lores frames from the bus with PyAV."""
        self._rcfg = rcfg
        if not rcfg.enabled:
            return
        try:
            import av  # noqa: F401
        except ImportError:
            log.warning("recording disabled: PyAV not installed (pip install av)")
            return
        from ..recording import BusClipWriter

        self.clip_writer = BusClipWriter(self.bus, rcfg.prebuffer_s, self.cfg.framerate)

    def clip_size(self) -> tuple[int, int]:
        """Resolution of recorded clips."""
        return tuple(self.cfg.lores_size)

    def start(self) -> None:
        self.started_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.clip_writer is not None and hasattr(self.clip_writer, "close"):
            self.clip_writer.close()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        backoff = 1.0
        failures = 0
        while not self._stop.is_set():
            seq_before = self.bus.latest().seq if self.bus.latest() else 0
            try:
                self._capture_loop()
                backoff, failures = 1.0, 0
            except Exception as exc:  # keep retrying: a camera can come back after a reseat
                self._error = f"{type(exc).__name__}: {exc}"
                got_frames = (self.bus.latest().seq if self.bus.latest() else 0) > seq_before
                failures = 0 if got_frames else failures + 1
                limit = self.max_consecutive_failures
                if limit is not None and failures >= limit:
                    log.error("camera %s failed %d times in a row (%s); giving up", self.name, failures, self._error)
                    self.fatal = True
                    return
                log.error("camera %s failed: %s; retrying in %.0fs", self.name, self._error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _capture_loop(self) -> None:
        raise NotImplementedError

    def health(self) -> CameraHealth:
        latest = self.bus.latest()
        age = None if latest is None else time.time() - latest.timestamp
        ok = age is not None and age < self.cfg.stale_after_s
        return CameraHealth(
            ok=ok,
            fps=self.bus.fps() if ok else 0.0,
            frame_age_s=age,
            frames=latest.seq if latest else 0,
            error=None if ok else (self._error or "no frames yet"),
            sensor=self.sensor,
        )
