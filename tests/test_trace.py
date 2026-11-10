"""Replaying a clip through the detectors for the panel's player."""

import json
import time

import pytest

cv2 = pytest.importorskip("cv2")
av = pytest.importorskip("av")

from fastapi.testclient import TestClient
from test_gates import ROI as GATE_ROI
from test_gates import arm

from wth_edge import config as cfgmod
from wth_edge.camera import FrameBus
from wth_edge.clips import ClipStore
from wth_edge.trace import Tracer, trace_clip
from wth_edge.web.app import create_app

CLIP = "20261110T161325Z_jf-pi-5_manual"


def make_clip(tmp_path, seconds=8, fps=10, codec="mpeg4"):
    """A clip whose barrier arm stands up for the first half and is gone for the second."""
    store = ClipStore(tmp_path)
    p = store.paths(CLIP)
    p["video"].parent.mkdir(parents=True)
    with av.open(str(p["video"]), "w") as c:
        s = c.add_stream(codec, rate=fps)
        s.width, s.height, s.pix_fmt = 1280, 720, "yuv420p"
        up, down = arm(90), arm(None)
        for i in range(seconds * fps):
            img = up if i < seconds * fps // 2 else down
            for pkt in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                c.mux(pkt)
        for pkt in s.encode():
            c.mux(pkt)
    store.write_meta({"id": CLIP, "label": "bridge", "started_at": 1, "duration_s": seconds,
                      "rois": {"gate_1": vars(GATE_ROI)}})
    return store


def config():
    return cfgmod.from_dict({"boat_detector": {"enabled": False}})


def test_trace_clip_records_every_frame(tmp_path):
    store = make_clip(tmp_path)
    cfg = config()
    tr = trace_clip(cfg, store.paths(CLIP)["video"], store.read_meta(CLIP), {"gate_1": GATE_ROI})
    frames = tr["frames"]
    assert len(frames) >= 15 and frames[0]["t"] == 0
    assert set(tr["rois"]) == {"gate_1"}
    first, last = frames[3]["gates"]["gate_1"], frames[-1]["gates"]["gate_1"]
    assert first["pos"] == "up" and first["line"] is not None and first["share"] > 0.7
    assert last["pos"] == "down"
    assert any(e["type"] == "gate" and e["position"] == "down" for e in tr["events"])
    assert cfg.stages.enabled  # the live config is left alone
    json.dumps(tr)


def test_trace_endpoints(tmp_path):
    store = make_clip(tmp_path)
    cfg = config()
    tracer = Tracer(cfg, store)
    client = TestClient(create_app(cfg, FrameBus(), dict, store=store, tracer=tracer))
    assert client.get(f"/api/clips/{CLIP}/trace").json()["state"] == "none"
    assert client.get(f"/clips/{CLIP}.trace").status_code == 404
    assert client.post(f"/api/clips/{CLIP}/trace").json()["state"] == "running"
    for _ in range(200):
        if client.get(f"/api/clips/{CLIP}/trace").json()["state"] == "done":
            break
        time.sleep(0.05)
    assert client.get(f"/api/clips/{CLIP}/trace").json()["state"] == "done"
    body = client.get(f"/clips/{CLIP}.trace").json()
    assert body["clip"] == CLIP and body["frames"]
    assert [m["id"] for m in store.list()] == [CLIP]  # the trace isn't taken for a clip
    assert client.post("/api/clips/20261110T000000Z_x_manual/trace").status_code == 404
    store.delete(CLIP)
    assert not store.paths(CLIP)["trace"].exists()
    tracer.close()


def test_frame_record_is_small():
    from wth_edge.trace import frame_record

    rec = frame_record(1.234, {"opening": {"phase": "flowing"}, "bridge": {"position": "closed", "angle_deg": 3.0},
                               "traffic": {"northbound": {"level": "light", "flow": 0.01234, "fill": 0.2}},
                               "vehicles": [{"box": [0.1, 0.2, 0.3, 0.4], "moving": True, "lane": "northbound"}],
                               "boat": {"present": False}})
    assert rec["t"] == 1.23 and rec["phase"] == "flowing" and rec["veh"] == [[0.1, 0.2, 0.3, 0.4, 1, "northbound"]]
    assert len(json.dumps(rec)) < 600
