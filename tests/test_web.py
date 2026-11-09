import numpy as np
from fastapi.testclient import TestClient

from wth_edge import config as cfgmod
from wth_edge.camera import FrameBus
from wth_edge.state import new_state
from wth_edge.web.app import create_app


def make_client(monkeypatch, password=""):
    monkeypatch.setenv("WTH_WEB_PASSWORD", password)
    cfg = cfgmod.from_dict({"site": {"rois": {"boat": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.2}}}})
    bus = FrameBus()
    state = new_state("t", "2.0")
    return TestClient(create_app(cfg, bus, lambda: state)), bus


def test_state_and_snapshot(monkeypatch):
    client, bus = make_client(monkeypatch)
    assert client.get("/snapshot.jpg").status_code == 503
    bus.publish(np.zeros((90, 160, 3), dtype=np.uint8))
    r = client.get("/snapshot.jpg")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    body = client.get("/api/state").json()
    assert body["device_id"] == "t" and body["auth_enabled"] is False and "boat" in body["rois"]
    assert client.get("/").status_code == 200


def test_greyscale_frames_encode(monkeypatch):
    client, bus = make_client(monkeypatch)
    bus.publish(np.zeros((90, 160), dtype=np.uint8))
    assert client.get("/snapshot.jpg").status_code == 200


def test_password_required_when_set(monkeypatch):
    client, _ = make_client(monkeypatch, password="s3cret")
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", auth=("wth", "wrong")).status_code == 401
    assert client.get("/api/state", auth=("wth", "s3cret")).status_code == 200
    assert client.get("/healthz").status_code == 200  # open for doctor


def test_roi_zoom(monkeypatch):
    client, bus = make_client(monkeypatch)
    assert client.get("/roi/boat.jpg").status_code == 503
    assert client.get("/roi/nope.jpg").status_code == 404
    assert client.get("/stream.mjpg?roi=nope").status_code == 404
    bus.publish(np.zeros((90, 160, 3), dtype=np.uint8))
    r = client.get("/roi/boat.jpg")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    import io

    from PIL import Image
    assert Image.open(io.BytesIO(r.content)).size == (80, 18)  # 0.5 x 0.2 of 160 x 90


def test_roi_zoom_crops_hires_yuv():
    from wth_edge.camera.base import Frame, HiRes
    from wth_edge.config import Roi
    from wth_edge.web.app import crop_roi

    w, h = 640, 360
    yuv = np.zeros((h * 3 // 2, w), dtype=np.uint8)
    yuv[:h, w // 2:] = 235  # right half of the luma plane bright
    yuv[h:] = 128  # neutral chroma
    frame = Frame(1, 0.0, np.zeros((90, 160, 3), dtype=np.uint8), HiRes(yuv, "yuv420", (w, h)))
    left = crop_roi(frame, Roi(0.0, 0.0, 0.4, 0.5))
    right = crop_roi(frame, Roi(0.6, 0.5, 0.4, 0.5))
    assert left.shape == (180, 256, 3) and right.shape == (180, 256, 3)
    assert left.mean() < 30 and right.mean() > 200


def test_detection_layer_draws_cars(monkeypatch):
    monkeypatch.setenv("WTH_WEB_PASSWORD", "")
    cfg = cfgmod.from_dict({"site": {"rois": {"northbound": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}}})
    bus = FrameBus()
    state = new_state("t", "2.0")
    state["vehicles"] = [{"box": [0.4, 0.4, 0.6, 0.6], "moving": True, "lane": "northbound"}]
    client = TestClient(create_app(cfg, bus, lambda: state))
    bus.publish(np.zeros((90, 160, 3), dtype=np.uint8))

    def bright(url):
        import io

        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(client.get(url).content)).convert("L")).max()

    assert bright("/snapshot.jpg?boxes=0&det=0") < 30
    assert bright("/snapshot.jpg?boxes=0&det=1") > 150  # the car box, in white
    assert bright("/roi/northbound.jpg") < 30
    assert bright("/roi/northbound.jpg?det=1") > 150
    assert state["vehicles"] and client.get("/snapshot.jpg?overlay=0").status_code == 200
