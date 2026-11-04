"""Raspberry Pi camera through Picamera2 (libcamera).

Configures two streams: ``main`` at full working resolution (used later by the H.264
recorder and live stream) and ``lores``, a small frame the preview and detectors read.
Picamera2 is only installable from apt (``python3-picamera2``), which is why the venv is
built with ``--system-site-packages``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import numpy as np

from .base import Camera, HiRes

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

    def setup_recording(self, rcfg) -> None:
        # The H.264 encoder can only start once the camera runs; see _configure().
        self._rcfg = rcfg

    def clip_size(self) -> tuple[int, int]:
        return tuple(self.cfg.main_size)

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
        if self._rcfg is not None and self._rcfg.enabled:
            try:
                self.clip_writer = PiClipWriter(picam, self._rcfg, cfg.framerate)
            except Exception:
                self.clip_writer = None
                log.exception("H.264 recorder unavailable; recording disabled")
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
            mw, mh = self.cfg.main_size
            while not self._stop.is_set():
                if self.cfg.detector_hires:
                    (lores, main), _ = self._picam.capture_arrays(["lores", "main"])
                    # The buffer is reused by the camera, so copy (about 1.4 MB at 720p).
                    self.bus.publish(self._to_image(lores), hires=HiRes(main.copy(), "yuv420", (mw, mh)))
                else:
                    self.bus.publish(self._to_image(self._picam.capture_array("lores")))
        finally:
            try:
                if self.clip_writer is not None:
                    self.clip_writer.close()
                    self.clip_writer = None
                self._picam.stop()
                self._picam.close()
            except Exception:  # best effort on shutdown
                log.debug("error closing camera", exc_info=True)
            self._picam = None


class PiClipWriter:
    """H.264 on the main stream into a circular buffer (Picamera2), written out as .mp4.

    The Pi 5 has no hardware H.264 encoder, so it uses libav (software); older Pis use
    the hardware encoder. Keyframes every second keep the pre-roll cut close to
    ``prebuffer_s``.
    """

    def __init__(self, picam, rcfg, fps: float) -> None:
        from picamera2 import encoders  # type: ignore[import-not-found]

        self.picam = picam
        self.rcfg = rcfg
        self._created = time.time()
        self.fps = fps
        iperiod = max(1, int(round(fps)))
        encoder = None
        try:
            from picamera2.platform import Platform, get_platform  # type: ignore[import-not-found]

            if get_platform() == Platform.PISP and hasattr(encoders, "LibavH264Encoder"):
                encoder = encoders.LibavH264Encoder(bitrate=rcfg.bitrate, iperiod=iperiod, framerate=fps)
        except ImportError:
            pass
        if encoder is None:
            encoder = encoders.H264Encoder(bitrate=rcfg.bitrate, repeat=True, iperiod=iperiod)

        from picamera2 import outputs  # type: ignore[import-not-found]

        self._mode = "circular2" if hasattr(outputs, "CircularOutput2") and hasattr(outputs, "PyavOutput") else "circular"
        if self._mode == "circular2":
            self.circ = outputs.CircularOutput2(buffer_duration_ms=int(rcfg.prebuffer_s * 1000))
        else:
            self.circ = outputs.CircularOutput(buffersize=int(rcfg.prebuffer_s * fps))
        self.encoder = encoder
        picam.start_encoder(encoder, self.circ, name="main")
        self._path: Path | None = None
        log.info("recorder ready: %s, %.0f s pre-roll (%s)", type(encoder).__name__, rcfg.prebuffer_s, self._mode)

    def begin(self, path: Path) -> float:
        from picamera2 import outputs  # type: ignore[import-not-found]

        self._path = Path(path)
        got = min(self.rcfg.prebuffer_s, time.time() - self._created)
        if self._mode == "circular2":
            self.circ.open_output(outputs.PyavOutput(str(self._path)))
        else:
            self.circ.fileoutput = str(self._path.with_suffix(".h264"))
            self.circ.start()
        return got

    def end(self) -> None:
        if self._path is None:
            return
        path, self._path = self._path, None
        if self._mode == "circular2":
            self.circ.close_output()
            return
        self.circ.stop()
        raw = path.with_suffix(".h264")
        # Older Picamera2: wrap the raw H.264 in an mp4 without re-encoding.
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(self.fps), "-i", str(raw), "-c", "copy", str(path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            raw.unlink(missing_ok=True)
        else:
            log.error("ffmpeg remux failed, keeping %s: %s", raw, result.stderr.strip())

    def close(self) -> None:
        try:
            self.end()
        finally:
            try:
                self.picam.stop_encoder()
            except Exception:
                log.debug("stop_encoder failed", exc_info=True)
