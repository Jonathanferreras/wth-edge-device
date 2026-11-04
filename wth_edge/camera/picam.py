"""Raspberry Pi camera through Picamera2 (libcamera).

Configures two streams: ``main`` at full working resolution (used later by the H.264
recorder and live stream) and ``lores``, a small frame the preview and detectors read.
Picamera2 is only installable from apt (``python3-picamera2``), which is why the venv is
built with ``--system-site-packages``.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import Camera

log = logging.getLogger(__name__)


def camera_info() -> list[dict]:
    """Cameras libcamera can see, without opening one. Empty when Picamera2 is missing."""
    try:
        from picamera2 import Picamera2
    except ImportError:
        return []
    return list(Picamera2.global_camera_info())


class PiCamera(Camera):
    name = "picamera2"
    # libcamera does not always recover inside one process after a failed open
    # ("Camera __init__ sequence did not complete"), so let systemd restart us.
    max_consecutive_failures = 3

    def __init__(self, cfg, bus) -> None:
        super().__init__(cfg, bus)
        self._picam = None

    def _open(self):
        from picamera2 import Picamera2  # type: ignore[import-not-found]

        cams = Picamera2.global_camera_info()
        if not cams:
            raise RuntimeError("no camera detected (check the ribbon cable and `rpicam-hello --list-cameras`)")
        self.sensor = cams[0].get("Model")

        try:
            picam = Picamera2(0)
        except RuntimeError as exc:
            # Picamera2 logs the underlying libcamera error just before this; the usual
            # cause is another process holding the camera (rpicam-vid, mediamtx, ...).
            raise RuntimeError(f"{exc} Is another process using the camera? (see journalctl -u wth-edge)") from exc
        try:
            return self._configure(picam)
        except Exception:
            picam.close()
            raise

    def _configure(self, picam):
        from libcamera import Transform, controls  # type: ignore[import-not-found]

        cfg = self.cfg
        video_config = picam.create_video_configuration(
            main={"size": tuple(cfg.main_size), "format": "YUV420"},
            lores={"size": tuple(cfg.lores_size), "format": cfg.lores_format},
            transform=Transform(hflip=int(cfg.hflip), vflip=int(cfg.vflip)),
            controls={"FrameRate": float(cfg.framerate)},
            buffer_count=4,
        )
        picam.configure(video_config)

        ctrl = {}
        if "AfMode" in picam.camera_controls:  # Camera Module 3 has autofocus, older ones don't
            mode = cfg.af_mode.lower()
            if mode == "manual":
                ctrl["AfMode"] = controls.AfModeEnum.Manual
                ctrl["LensPosition"] = float(cfg.lens_position)
            elif mode == "continuous":
                ctrl["AfMode"] = controls.AfModeEnum.Continuous
            elif mode == "auto":
                ctrl["AfMode"] = controls.AfModeEnum.Auto
        if ctrl:
            picam.set_controls(ctrl)
        picam.start()
        log.info("camera %s started: main %s, lores %s %s @ %.0f fps",
                 self.sensor, cfg.main_size, cfg.lores_size, cfg.lores_format, cfg.framerate)
        return picam

    def _to_image(self, arr: np.ndarray) -> np.ndarray:
        fmt = self.cfg.lores_format.upper()
        if fmt in ("RGB888", "XRGB8888"):
            # libcamera's "RGB888" is laid out B, G, R in memory.
            return np.ascontiguousarray(arr[..., 2::-1])
        if fmt in ("BGR888", "XBGR8888"):
            return np.ascontiguousarray(arr[..., :3])
        # YUV420: the first `height` rows are the Y (luma) plane.
        h = self.cfg.lores_size[1]
        return np.ascontiguousarray(arr[:h, : self.cfg.lores_size[0]])

    def _capture_loop(self) -> None:
        self._picam = self._open()
        self._error = None
        try:
            while not self._stop.is_set():
                arr = self._picam.capture_array("lores")
                self.bus.publish(self._to_image(arr))
        finally:
            try:
                self._picam.stop()
                self._picam.close()
            except Exception:  # best effort on shutdown
                log.debug("error closing camera", exc_info=True)
            self._picam = None
