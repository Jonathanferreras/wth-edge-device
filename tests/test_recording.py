import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

av = pytest.importorskip("av")

from wth_edge import config as cfgmod  # noqa: E402
from wth_edge.camera import FrameBus  # noqa: E402
from wth_edge.camera.fake import FakeCamera  # noqa: E402
from wth_edge.clips import ClipStore  # noqa: E402
from wth_edge.importer import import_dataset  # noqa: E402
from wth_edge.recording import Recorder, RecordingError  # noqa: E402
from wth_edge.state import new_state  # noqa: E402
from wth_edge.web.app import create_app  # noqa: E402


def make(tmp_path, prebuffer=0.5):
    cfg = cfgmod.from_dict({
        "device": {"id": "test-01"},
        "camera": {"driver": "fake", "framerate": 15, "lores_size": [160, 90]},
        "paths": {"data_dir": str(tmp_path)},
        "recording": {"prebuffer_s": prebuffer, "max_duration_s": 30},
        "site": {"rois": {"boat": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.2}}},
    })
    bus = FrameBus()
    cam = FakeCamera(cfg.camera, bus)
    cam.setup_recording(cfg.recording)
    cam.start()
    store = ClipStore(tmp_path)
    return cfg, bus, cam, store, Recorder(cfg, cam, store)


def frames_in(path):
    with av.open(str(path)) as c:
        return sum(1 for _ in c.decode(video=0))


def test_record_clip_with_prebuffer(tmp_path):
    cfg, bus, cam, store, rec = make(tmp_path)
    try:
        time.sleep(0.8)  # fill the pre-roll
        meta = rec.start(label="boat", note="test")
        assert rec.status()["recording"] and rec.status()["label"] == "boat"
        with pytest.raises(RecordingError):
            rec.start()
        time.sleep(0.6)
        done = rec.stop()
    finally:
        cam.stop()
    paths = store.paths(meta["id"])
    assert paths["video"].stat().st_size > 0 and paths["thumb"].exists()
    saved = json.loads(paths["meta"].read_text())
    assert saved["label"] == "boat" and saved["ended_at"] and saved["duration_s"] == done["duration_s"]
    assert saved["rois"]["boat"]["w"] == 0.5
    # ~0.5 s pre-roll + ~0.6 s live at 15 fps; generous bounds for slow CI
    assert frames_in(paths["video"]) >= 8


def test_store_list_update_delete_prune(tmp_path):
    store = ClipStore(tmp_path)
    for i, ts in enumerate((1_700_000_000, 1_700_000_100)):
        store.write_meta({"id": f"2023111{i}T000000Z_t_manual", "started_at": ts, "keep": i == 0})
    clips = store.list()
    assert [c["started_at"] for c in clips] == [1_700_000_100, 1_700_000_000]
    assert store.update(clips[0]["id"], label="boat")["label"] == "boat"
    # Asking for 101% free deletes every clip that isn't kept.
    deleted = store.prune(101)
    assert deleted == ["20231111T000000Z_t_manual"]
    assert [c["id"] for c in store.list()] == ["20231110T000000Z_t_manual"]
    assert store.delete("20231110T000000Z_t_manual") and store.list() == []
    with pytest.raises(ValueError):
        store.paths("../etc/passwd")


def test_api_record_and_clips(tmp_path, monkeypatch):
    monkeypatch.setenv("WTH_WEB_PASSWORD", "")
    cfg, bus, cam, store, rec = make(tmp_path, prebuffer=0.2)
    client = TestClient(create_app(cfg, bus, lambda: new_state("t", "2"), recorder=rec, store=store))
    try:
        time.sleep(0.4)
        assert client.post("/api/recording/start", json={"label": "nope"}).status_code == 400
        r = client.post("/api/recording/start", json={"label": "bridge", "note": "hi"})
        assert r.status_code == 200
        clip_id = client.get("/api/recording").json()["clip_id"]
        listed = client.get("/api/clips").json()["clips"]
        assert listed[0]["id"] == clip_id and listed[0]["recording"]
        assert client.delete(f"/api/clips/{clip_id}").status_code == 409
        time.sleep(0.4)
        assert client.post("/api/recording/stop").status_code == 200
    finally:
        cam.stop()
    assert client.get(f"/clips/{clip_id}.mp4").status_code == 200
    assert client.get(f"/clips/{clip_id}.jpg").content[:2] == b"\xff\xd8"
    assert client.patch(f"/api/clips/{clip_id}", json={"keep": True}).json()["keep"] is True
    assert client.delete(f"/api/clips/{clip_id}").status_code == 200
    assert client.get(f"/clips/{clip_id}.mp4").status_code == 404


def test_put_rois_saves_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WTH_WEB_PASSWORD", "")
    cfg = cfgmod.from_dict({"paths": {"data_dir": str(tmp_path)}, "site": {"rois": {"boat": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.2}}}})
    client = TestClient(create_app(cfg, FrameBus(), lambda: new_state("t", "2")))
    bad = {"boat": {"x": 0.8, "y": 0.1, "w": 0.5, "h": 0.2}}
    assert client.put("/api/rois", json=bad).status_code == 400
    good = {"boat": {"x": 0.2, "y": 0.3, "w": 0.5, "h": 0.2}}
    assert client.put("/api/rois", json=good).status_code == 200
    assert cfg.site.rois["boat"].x == 0.2
    assert "[rois.boat]" in (tmp_path / cfgmod.ROI_OVERRIDE_FILE).read_text()


def test_import_dataset(tmp_path):
    src = tmp_path / "dataset" / "boat_crossings" / "04-03-2026" / "boat1"
    (src / "e2w").mkdir(parents=True)
    (src / "boat_details.json").write_text(json.dumps({"name": "Test Barge"}))
    with av.open(str(src / "e2w" / "04-03-2026_000.mp4"), "w") as c:
        s = c.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = 64, 48, "yuv420p"
        for _ in range(10):
            for p in s.encode(av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    store = ClipStore(tmp_path / "data")
    out = import_dataset(tmp_path / "dataset", store)
    assert len(out) == 1
    m = out[0]
    assert m["id"].startswith("20260403T000000Z_import_boat_") and m["keep"]
    assert m["import"]["direction"] == "e2w" and m["import"]["boat_details"]["name"] == "Test Barge"
    assert store.paths(m["id"])["thumb"].exists()
    assert import_dataset(tmp_path / "dataset", store) == []  # idempotent
