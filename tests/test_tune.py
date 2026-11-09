import json
import time

import numpy as np
import pytest

from wth_edge import config as cfgmod
from wth_edge.clips import ClipStore
from wth_edge.tune import analysis, reference

ROIS = {
    "northbound": {"x": 0.05, "y": 0.55, "w": 0.4, "h": 0.2},
    "southbound": {"x": 0.55, "y": 0.55, "w": 0.4, "h": 0.2},
    "deck": {"x": 0.3, "y": 0.4, "w": 0.4, "h": 0.1},
    "bridge": {"x": 0.3, "y": 0.1, "w": 0.4, "h": 0.25},
}


def make_cfg(tmp_path, **tune):
    return cfgmod.from_dict({
        "device": {"id": "test-01"},
        "camera": {"driver": "fake", "framerate": 10, "lores_size": [160, 90], "detector_hires": False},
        "paths": {"data_dir": str(tmp_path)},
        "recording": {"max_duration_s": 600, "min_free_percent": 0},
        "boat_detector": {"model_path": str(tmp_path / "no-model.onnx")},
        "tune": {"sample_s": 4, "pair_gap_s": 1, "max_reviews": 40, **tune},
        "site": {"rois": ROIS},
    })


def entry(t, nb="light", nb_flow=0.01, nb_fill=0.05, sb="light", deck=True, bridge="closed", angle=5.0, boat=False):
    return {
        "t": t,
        "traffic": {"northbound": {"level": nb, "flow": nb_flow, "fill": nb_fill},
                    "southbound": {"level": sb, "flow": 0.01, "fill": 0.05}},
        "deck": {"clear": deck, "fill": 0.01},
        "bridge": {"position": bridge, "angle_deg": angle},
        "boat": {"present": boat},
    }


# ---------------------------------------------------------------- reference helpers
def test_live_summary_maps_the_compared_fields():
    s = reference.live_summary(entry(3, nb="standstill", bridge="open"))
    assert s == {"northbound": "standstill", "southbound": "light", "deck": True, "bridge": "raised", "boat": False}
    e = entry(1)
    e["far_leaf"] = {"position": "unknown"}
    assert reference.live_summary(e)["bridge"] == "closed"
    e["bridge"]["position"] = "unknown"
    assert reference.live_summary(e)["bridge"] == "unknown"


def test_change_times_and_samples():
    entries = [entry(t) for t in range(10)] + [entry(t, nb="standstill") for t in range(10, 20)]
    assert reference.change_times(entries) == [10.0]
    picks = reference.pick_samples(0, 19, 5, [10.0], cap=10)
    assert 10.0 in picks
    assert all(b - a >= 4 for a, b in zip(picks, picks[1:]))
    assert len(reference.pick_samples(0, 600, 5, [], cap=6)) == 6


def test_tracker_counts_cars_once_and_boats_with_direction():
    tr = reference.Tracker(max_gap=2.0, max_jump=1.5)
    for i in range(5):  # one car driving right through northbound
        tr.update(i * 0.5, [("northbound", 0.6, (0.1 + i * 0.03, 0.6, 0.15 + i * 0.03, 0.65), "car")])
    tr.update(10, [("southbound", 0.3, (0.6, 0.6, 0.65, 0.65), "car")])  # one weak single sighting
    done = tr.finish()
    assert [t.tag for t in done if reference.counted(t)] == ["northbound"]
    b = reference.Tracker(max_gap=20, max_jump=0.2, absolute=True)
    for i in range(4):
        b.update(i * 2.0, [("boat", 0.7, (0.2 + i * 0.05, 0.2, 0.3 + i * 0.05, 0.3), "boat")])
    (boat,) = b.finish()
    assert reference.boat_direction(boat, "WEST", "EAST", 0.03) == "w2e"


# ---------------------------------------------------------------- analysis
def test_best_threshold_separates_and_needs_data():
    assert analysis.best_threshold([0.01, 0.02, 0.03], [0.001, 0.002], 0.003) is None  # too few below
    cut = analysis.best_threshold([0.004, 0.005, 0.006], [0.0005, 0.001, 0.0015], 0.01)
    assert 0.0015 < cut < 0.004
    # Already separating: no change.
    assert analysis.best_threshold([0.004, 0.005, 0.006], [0.0005, 0.001, 0.0015], 0.003) is None


def lane(motion, amount="light"):
    return {"motion": motion, "amount": amount}


def label(nb="moving", sb="moving", deck="clear", bridge="down", boat="no"):
    return {"northbound": lane(nb), "southbound": lane(sb), "deck": deck, "bridge": bridge, "boat": boat, "note": ""}


def test_score_counts_matches_with_a_little_lag():
    logs = {"c": [entry(t, nb="standstill" if t >= 13 else "light") for t in range(30)]}
    samples = [
        {"clip_id": "c", "t": 2, "label": label()},                      # all right
        {"clip_id": "c", "t": 10, "label": label(nb="stopped")},         # detector catches up within the lag
        {"clip_id": "c", "t": 20, "label": label(nb="moving")},          # wrong: still standstill
        {"clip_id": "c", "t": 24, "label": label(nb="unclear", boat="unclear")},
        {"clip_id": "c", "t": 26},                                        # no review
    ]
    sc = analysis.score(samples, logs, gap=1)
    nb = sc["fields"]["northbound"]
    assert (nb["n"], nb["ok"]) == (3, 2)
    assert sc["fields"]["boat"]["n"] == 3
    assert 0 < sc["accuracy"] < 1


def test_fit_moves_flow_min_toward_the_review(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.traffic_detector.flow_min = 0.003
    # The lane really moves at flow 0.002 (the detector calls it stopped), and is stopped at 0.0005.
    logs = {"c": [entry(t, nb_flow=0.002 if t < 20 else 0.0005) for t in range(40)]}
    samples = [{"clip_id": "c", "t": t, "label": label(nb="moving" if t < 19 else "stopped")} for t in range(0, 38, 3)]
    changes = analysis.fit(samples, logs, cfg, gap=1)
    flow = [c for c in changes if c["key"] == "traffic_detector.flow_min"]
    assert flow and 0.0005 < flow[0]["new"] < 0.002


def test_tuning_file_applies_and_survives_reload(tmp_path):
    cfg = make_cfg(tmp_path)
    cfgmod.save_tuning(cfg, {"traffic_detector.flow_min": 0.0021, "bridge_detector.closed_max_deg": 20})
    dev = tmp_path / "device.toml"
    dev.write_text(f'[device]\nid = "t"\n[paths]\ndata_dir = "{tmp_path}"\n')
    loaded = cfgmod.load(dev)
    assert loaded.traffic_detector.flow_min == pytest.approx(0.0021)
    assert loaded.bridge_detector.closed_max_deg == 20.0
    assert loaded.untuned["traffic_detector.flow_min"] == cfgmod.TrafficDetectorSection().flow_min
    # Anything not tunable is ignored.
    assert cfgmod.apply_tuning(loaded, {"recording": {"enabled": 0}}) == {}


# ---------------------------------------------------------------- the session, end to end
av = pytest.importorskip("av")
pytest.importorskip("cv2")


def write_video(path, seconds=12, fps=10, size=(320, 180)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as c:
        s = c.add_stream("mpeg4", rate=fps)
        s.width, s.height, s.pix_fmt = size[0], size[1], "yuv420p"
        for i in range(seconds * fps):
            img = np.full((size[1], size[0], 3), 90, np.uint8)
            x = int(20 + (i * 3) % 100)
            img[105:125, x:x + 16] = 230  # a "car" moving through northbound
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for p in s.encode(frame):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


class FakeRecorder:
    """Writes a synthetic clip and a detection log that says northbound is stopped."""

    def __init__(self, store, rois):
        self.store, self.rois = store, rois
        self.available, self.reserved_by, self.active_trigger = True, None, None
        self._meta = None
        self.clock = time.time() - 3600  # each clip starts where the last one ended

    def start(self, trigger, label=None, note=""):
        now = self.clock
        self.clock += 12
        cid = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}{len(list(self.store.iter_meta())):02d}Z_t_{trigger}"
        self._meta = {"id": cid, "trigger": trigger, "label": label, "note": note, "keep": False,
                      "started_at": now, "prebuffer_s": 0.0, "rois": self.rois, "detections": []}
        self.store.write_meta(self._meta)
        self.active_trigger = trigger
        return dict(self._meta)

    def stop(self, by_user=False):
        m = self._meta
        write_video(self.store.paths(m["id"])["video"])
        m["detections"] = [entry(t, nb="standstill", nb_flow=0.002) for t in range(12)]
        m["detections"].append({"t": 3.0, "event": {"type": "traffic", "at": m["started_at"] + 3, "direction": "northbound",
                                                    "level": "standstill"}})
        m["duration_s"] = 12.0
        m["ended_at"] = m["started_at"] + 12
        self.store.write_meta(m)
        self.active_trigger = None
        self._meta = None
        return dict(m)


class FakeReviewer:
    def __init__(self, tcfg, client=None):
        self.tcfg = tcfg
        self.calls = 0

    def label(self, a, b, boxes):
        assert a.is_file() and b.is_file()
        self.calls += 1
        return label(nb="moving")

    def usage(self):
        return {"model": "fake", "calls": self.calls, "failures": 0, "input_tokens": 0, "output_tokens": 0}


def test_round_records_reviews_reports_and_cleans_up(tmp_path, monkeypatch):
    from wth_edge.tune import review, session

    cfg = make_cfg(tmp_path, chunk_s=12, duration_s=24)
    store = ClipStore(tmp_path)
    rec = FakeRecorder(store, ROIS)
    monkeypatch.setattr(review, "available", lambda tcfg: (True, "fake"))
    monkeypatch.setattr(review, "ClaudeReviewer", FakeReviewer)
    tuner = session.Tuner(cfg, rec, store)
    tuner._wait = lambda s: None
    out = tuner._round("20261109T190000Z", 1, 24.0)
    assert out["status"] == "done"
    (rep,) = tuner.reports()
    r = tuner.report(rep["id"])
    assert r["chunks"] == 2 and r["reviewed"] >= 5
    # Claude saw moving cars, the detector said standstill: northbound is all wrong.
    assert r["accuracy"]["live"]["fields"]["northbound"]["accuracy"] == 0.0
    assert r["counts"]["standstills"]["northbound"] == 2
    assert r["disagreements"] and r["disagreements"][0]["fields"][0] == "northbound"
    # Footage gone, only the disagreement frames kept, auto-recording released.
    assert not [m for m in store.iter_meta()]
    kept = {p.name for p in (tmp_path / "tune" / rep["id"] / "frames").iterdir()}
    assert kept == {d["a"] for d in r["disagreements"]} | {d["b"] for d in r["disagreements"]}
    assert tuner.frame_path(rep["id"], r["disagreements"][0]["a"]) is not None
    assert tuner.frame_path(rep["id"], "../report.json") is None
    assert rec.reserved_by is None
    json.dumps(r)


def test_apply_and_reset_tuning(tmp_path):
    from wth_edge.tune import session

    cfg = make_cfg(tmp_path)
    tuner = session.Tuner(cfg, FakeRecorder(ClipStore(tmp_path), ROIS), ClipStore(tmp_path))
    before = cfg.traffic_detector.flow_min
    tuner._apply([{"key": "traffic_detector.flow_min", "old": before, "new": 0.0017}], "x-r1")
    assert cfg.traffic_detector.flow_min == 0.0017
    assert cfgmod.tuning_override_path(cfg).is_file()
    assert tuner.tuned() == [{"key": "traffic_detector.flow_min", "base": before, "now": 0.0017}]
    tuner.reset_tuning()
    assert cfg.traffic_detector.flow_min == before and not cfgmod.tuning_override_path(cfg).exists()


def test_start_refuses_without_recording(tmp_path):
    from wth_edge.tune import session

    cfg = make_cfg(tmp_path)
    rec = FakeRecorder(ClipStore(tmp_path), ROIS)
    rec.available = False
    with pytest.raises(session.TuneError, match="recording is off"):
        session.Tuner(cfg, rec, ClipStore(tmp_path)).start(30)


def test_replay_runs_both_settings_over_the_chunks(tmp_path):
    cfg = make_cfg(tmp_path)
    video = tmp_path / "clip.mp4"
    write_video(video, seconds=6)
    meta = {"id": "c", "started_at": 1000.0, "prebuffer_s": 0.0}
    rois = {k: cfgmod.Roi(**v) for k, v in ROIS.items()}
    changes = [{"key": "traffic_detector.flow_min", "old": 0.003, "new": 0.5}]
    before, after = analysis.replay([(meta, video, rois)], [analysis.variant(cfg), analysis.variant(cfg, changes)])
    assert len(before["c"]) >= 5 and len(after["c"]) == len(before["c"])
    assert before["c"][-1]["traffic"]["northbound"]["level"] in ("light", "moderate", "heavy", "standstill")
    # The changed copy is separate from the live config.
    assert cfg.traffic_detector.flow_min == 0.003
    assert analysis.variant(cfg).boat_detector.enabled is False


def test_reference_model_runs_on_a_frame():
    path = cfgmod.REPO_ROOT / "models" / "yolov8s.onnx"
    if not path.is_file():
        pytest.skip("no model file")
    m = reference.YoloObjects(path)
    out = m.detect(np.full((360, 640, 3), 100, np.uint8), cfgmod.Roi(0, 0, 1, 1), reference.VEHICLE_CLASSES)
    assert out == [] and m.looks == 1


def test_round_fits_replays_and_decides(tmp_path, monkeypatch):
    from wth_edge.tune import review, session

    class SplitReviewer(FakeReviewer):
        def label(self, a, b, boxes):
            t = float(a.name.rsplit("_", 1)[1][:-5])
            return label(nb="moving" if t < 6 else "stopped")

    class SplitRecorder(FakeRecorder):
        def stop(self, by_user=False):
            m = super().stop(by_user)
            m["detections"] = [entry(t, nb="standstill", nb_flow=0.002 if t < 6 else 0.0005) for t in range(12)]
            self.store.write_meta(m)
            return m

    cfg = make_cfg(tmp_path, chunk_s=12, duration_s=24, sample_s=2, min_gain=-1.0)
    store = ClipStore(tmp_path)
    monkeypatch.setattr(review, "available", lambda tcfg: (True, "fake"))
    monkeypatch.setattr(review, "ClaudeReviewer", SplitReviewer)
    tuner = session.Tuner(cfg, SplitRecorder(store, ROIS), store)
    tuner._wait = lambda s: None
    tuner._round("20261109T190000Z", 1, 24.0)
    r = tuner.report(tuner.reports()[0]["id"])
    assert any(c["key"] == "traffic_detector.flow_min" for c in r["changes"])
    assert r["accuracy"]["replay"] is not None
    # min_gain -1 always applies: the live config and tuning.toml now hold the new value.
    assert r["applied"] and "Applied" in r["decision"]
    new = next(c["new"] for c in r["changes"] if c["key"] == "traffic_detector.flow_min")
    assert cfg.traffic_detector.flow_min == new
    assert cfgmod.tuning_override_path(cfg).is_file()


def test_panel_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from wth_edge.camera import FrameBus
    from wth_edge.state import new_state
    from wth_edge.tune import review, session
    from wth_edge.web.app import create_app

    monkeypatch.setenv("WTH_WEB_PASSWORD", "")
    monkeypatch.setattr(review, "available", lambda tcfg: (False, "no key"))
    cfg = make_cfg(tmp_path)
    store = ClipStore(tmp_path)
    rec = FakeRecorder(store, ROIS)
    rec.available = False
    tuner = session.Tuner(cfg, rec, store)
    client = TestClient(create_app(cfg, FrameBus(), lambda: new_state("t", "2"), store=store, tuner=tuner))
    st = client.get("/api/tune").json()
    assert st["active"] is False and st["reviewer"] == {"ok": False, "why": "no key"} and st["reports"] == []
    r = client.post("/api/tune/start", json={"minutes": 30})
    assert r.status_code == 409 and "recording is off" in r.json()["detail"]
    assert client.post("/api/tune/start", json={"minutes": "x"}).status_code == 400
    assert client.get("/api/tune/reports/nope").status_code == 404
    assert client.get("/tune/x-r1/..%2Freport.json").status_code == 404
    tuner._apply([{"key": "traffic_detector.flow_min", "old": 0.003, "new": 0.002}], "x-r1")
    assert client.post("/api/tune/reset").json()["undone"][0]["now"] == 0.002
    assert cfg.traffic_detector.flow_min == 0.003
