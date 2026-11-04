from __future__ import annotations

from .base import Camera, CameraHealth, Frame, FrameBus


def make_camera(cfg, bus: FrameBus) -> Camera:
    driver = cfg.driver.lower()
    if driver == "picamera2":
        from .picam import PiCamera

        return PiCamera(cfg, bus)
    if driver == "fake":
        from .fake import FakeCamera

        return FakeCamera(cfg, bus)
    if driver == "file":
        from .fake import FileCamera

        return FileCamera(cfg, bus)
    raise ValueError(f"unknown camera.driver {cfg.driver!r} (picamera2, fake, file)")


__all__ = ["Camera", "CameraHealth", "Frame", "FrameBus", "make_camera"]
