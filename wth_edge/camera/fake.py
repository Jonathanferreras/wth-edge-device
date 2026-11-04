"""A synthetic camera for laptops and tests: a moving gradient with a frame counter bar."""

from __future__ import annotations

import time

import numpy as np

from .base import Camera, HiRes


class FakeCamera(Camera):
    name = "fake"

    def _capture_loop(self) -> None:
        self.sensor = "fake"
        self._error = None
        w, h = self.cfg.lores_size
        xs = np.linspace(0, 255, w, dtype=np.float32)
        ys = np.linspace(0, 255, h, dtype=np.float32)[:, None]
        period = 1.0 / max(self.cfg.framerate, 0.1)
        n = 0
        while not self._stop.is_set():
            shift = (n * 4) % 256
            img = np.empty((h, w, 3), dtype=np.uint8)
            img[..., 0] = ((xs + shift) % 256).astype(np.uint8)
            img[..., 1] = ys.astype(np.uint8)
            img[..., 2] = 96
            bar = int((n % 100) / 100 * w)
            img[h - 8 :, :bar] = 255
            self.bus.publish(img)
            n += 1
            self._stop.wait(period)


class FileCamera(Camera):
    """Replays a recorded clip (needs opencv: ``pip install -e .[replay]``)."""

    name = "file"

    def _capture_loop(self) -> None:
        import cv2  # optional dependency

        path = self.cfg.file_path
        if not path:
            raise RuntimeError("camera.file_path is not set")
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open {path}")
        self.sensor = f"file:{path}"
        self._error = None
        fps = cap.get(cv2.CAP_PROP_FPS) or self.cfg.framerate
        period = 1.0 / max(fps, 0.1)
        w, h = self.cfg.lores_size
        try:
            while not self._stop.is_set():
                t0 = time.time()
                ok, bgr = cap.read()
                if not ok:
                    if not self.cfg.file_loop:
                        self._stop.wait(1.0)
                        continue
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                rgb = cv2.cvtColor(cv2.resize(bgr, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
                hires = HiRes(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), "rgb", (bgr.shape[1], bgr.shape[0])) if self.cfg.detector_hires else None
                self.bus.publish(rgb, hires=hires)
                self._stop.wait(max(0.0, period - (time.time() - t0)))
        finally:
            cap.release()
