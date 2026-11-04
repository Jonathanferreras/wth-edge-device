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
