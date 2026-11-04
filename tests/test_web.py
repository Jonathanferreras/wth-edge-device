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
