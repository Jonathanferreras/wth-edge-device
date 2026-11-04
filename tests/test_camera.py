import time

from wth_edge.camera import FrameBus, make_camera
from wth_edge.config import CameraSection


def test_fake_camera_feeds_the_bus_and_reports_health():
    bus = FrameBus()
    cam = make_camera(CameraSection(driver="fake", lores_size=(64, 36), framerate=50), bus)
    cam.start()
    try:
        frame = bus.wait_newer(0, timeout=2)
        assert frame is not None and frame.image.shape == (36, 64, 3)
        time.sleep(0.3)
        health = cam.health()
        assert health.ok and health.fps > 5
    finally:
        cam.stop()


def test_health_before_any_frame():
    cam = make_camera(CameraSection(driver="fake"), FrameBus())
    h = cam.health()
    assert not h.ok and h.error == "no frames yet"


def test_camera_gives_up_after_repeated_open_failures():
    from wth_edge.camera.base import Camera

    class Broken(Camera):
        name = "broken"
        max_consecutive_failures = 2

        def _capture_loop(self):
            raise RuntimeError("Camera __init__ sequence did not complete.")

    cam = Broken(CameraSection(), FrameBus())
    cam._stop.wait = lambda *_: False  # skip backoff sleeps
    cam._run()
    assert cam.fatal and "did not complete" in cam.health().error
