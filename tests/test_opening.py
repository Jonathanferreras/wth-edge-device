import json

import numpy as np
import pytest

from wth_edge import config as cfgmod
from wth_edge.ais import AisSource, describe, distance_bearing
from wth_edge.autorecord import AutoRecorder
from wth_edge.opening import OpeningHistory, OpeningMachine, Signals

BRIDGE = (40.8660, -73.8210)


# ---------------- state machine


def machine(tmp_path=None, **over):
    cfg = cfgmod.OpeningSection(**over)
    hist = OpeningHistory(tmp_path / "openings.json" if tmp_path else None, cfg.history,
                          {"gates_down": 60, "lifting": 120, "raised": 60, "lowering": 120, "releasing": 30})
    return OpeningMachine(cfg, cfgmod.BridgeDetectorSection(), ("east",), hist)


def run(m, steps, dt=1.0, t0=0.0):
    """steps: list of (seconds, Signals). Returns the phase events."""
    events, t = [], t0
    for secs, s in steps:
        for _ in range(int(secs / dt)):
            events += m.update(t, s)
            t += dt
    return events, t


def flowing(nb="light", sb="moderate"):
    return Signals(northbound=nb, northbound_confidence=0.8, southbound=sb, southbound_confidence=0.8,
                   northbound_moving=True, southbound_moving=True, deck_clear=False, deck_moving=True,
                   leaf_angles={"near": 5, "far": 6})


def stopped(boat=False):
    return Signals(northbound="standstill", northbound_confidence=0.8, southbound="standstill", southbound_confidence=0.8,
                   northbound_moving=False, southbound_moving=False, deck_clear=True, deck_moving=False,
                   leaf_angles={"near": 5, "far": 6}, boat_present=boat, boat_direction="e2w" if boat else None)


def leaves(near, far, rate_near=0.0, rate_far=0.0, deck_clear=True):
    return Signals(northbound="standstill", southbound="standstill", northbound_moving=False, southbound_moving=False,
                   deck_clear=deck_clear, leaf_angles={"near": near, "far": far}, leaf_rates={"near": rate_near, "far": rate_far},
                   northbound_confidence=0.8, southbound_confidence=0.8)


def test_full_opening_cycle(tmp_path):
    m = machine(tmp_path)
    events, t = run(m, [
        (5, flowing()),
        (30, stopped()),                       # gates down after 20 s
        (10, stopped(boat=True)),              # boat confirms it
        (60, leaves(5, 40, rate_far=1.5)),     # far leaf lifting
        (60, leaves(40, 80, rate_near=1.5)),   # near leaf lifting
        (30, leaves(80, 82)),                  # both up
        (60, leaves(40, 80, rate_near=-1.5)),  # lowering
        (10, leaves(5, 6)),                    # flat
        (20, Signals(northbound="heavy", southbound="heavy", northbound_moving=True, southbound_moving=True,
                     deck_clear=False, deck_moving=True, leaf_angles={"near": 5, "far": 6},
                     northbound_confidence=0.8, southbound_confidence=0.8)),
    ])
    assert [e["phase"] for e in events] == ["flowing", "gates_down", "lifting", "raised", "lowering", "releasing", "flowing"]
    gd = next(e for e in events if e["phase"] == "gates_down")
    assert 24 <= gd["at"] <= 26  # 5 s flowing + 20 s hold
    assert m.boat_direction == "e2w" and m.boat_source == "camera"
    # The opening was learned, with per-phase durations.
    saved = json.loads((tmp_path / "openings.json").read_text())
    assert len(saved) == 1 and saved[0]["boat_direction"] == "e2w"
    assert 55 <= saved[0]["phases"]["lifting"] <= 125 and "lowering" in saved[0]["phases"]


def test_website_fields_follow_the_invariants(tmp_path):
    m = machine(tmp_path)
    run(m, [(5, flowing()), (30, stopped(boat=True))])
    w = m.website()
    assert w["position"] == "closed" and w["estimatedWaitTime"] is None
    assert w["northBoundTraffic"] == "standstill"
    assert m.confidence == 0.8  # boat seen: "high"
    run(m, [(10, leaves(5, 40, rate_far=2.0))], t0=40)
    w = m.website()
    assert w["position"] == "opening"
    assert w["northBoundTraffic"] == "standstill" and w["southBoundTraffic"] == "standstill"
    wait = w["estimatedWaitTime"]
    assert wait and wait["status"] == "estimated" and wait["remainingMinutes"] > 0
    assert wait["initialTotalMinutes"] == pytest.approx((60 + 120 + 60 + 120 + 30) / 60, 0.01)


def test_false_alarm_returns_to_flowing():
    m = machine(false_alarm_s=100)
    events, _ = run(m, [(5, flowing()), (130, stopped())])
    assert [e["phase"] for e in events] == ["flowing", "gates_down", "flowing"]
    assert events[-1]["at"] - events[1]["at"] == pytest.approx(100, abs=1.5)


def test_traffic_moving_again_without_boat_or_lift_is_not_an_opening():
    m = machine()
    events, _ = run(m, [(5, flowing()), (30, stopped()), (15, flowing())])
    assert [e["phase"] for e in events] == ["flowing", "gates_down", "flowing"]
    assert m.history.openings == []


def test_boat_does_not_start_an_opening():
    m = machine()
    s = flowing()
    s.boat_present, s.boat_direction = True, "w2e"
    events, _ = run(m, [(5, flowing()), (60, s)])
    assert [e["phase"] for e in events] == ["flowing"]


def test_leaves_rising_with_no_traffic_sign_still_opens():
    m = machine()
    s = leaves(5, 30, rate_far=2.0)
    s.northbound = s.southbound = "light"
    events, _ = run(m, [(5, flowing()), (10, s)])
    assert [e["phase"] for e in events] == ["flowing", "lifting"]


def test_lifting_straight_to_lowering_when_raised_is_skipped():
    m = machine()
    events, _ = run(m, [(5, flowing()), (30, stopped()), (30, leaves(5, 60, rate_far=2.0)),
                        (30, leaves(5, 40, rate_far=-2.0)), (10, leaves(5, 6)), (15, flowing("heavy", "heavy"))])
    assert [e["phase"] for e in events] == ["flowing", "gates_down", "lifting", "lowering", "releasing", "flowing"]


def test_ais_confirms_and_sets_direction():
    m = machine()
    s = stopped()
    s.ais_side, s.ais_eta_s, s.ais_name = "west", 240.0, "TUG"
    run(m, [(5, flowing()), (30, s)])
    assert m.phase == "gates_down" and m.boat_direction == "w2e" and m.boat_source == "ais"
    assert m.confidence == 0.8


def test_unknown_after_silence_and_recovery():
    m = machine(unknown_after_s=10)
    events, t = run(m, [(5, flowing()), (20, Signals())])
    assert [e["phase"] for e in events] == ["flowing", "unknown"]
    events, _ = run(m, [(3, flowing())], t0=t)
    assert [e["phase"] for e in events] == ["flowing"]


def test_wait_uses_learned_medians(tmp_path):
    hist = OpeningHistory(tmp_path / "o.json", 30, {"gates_down": 60, "lifting": 120, "raised": 60, "lowering": 120, "releasing": 30})
    for d in (100, 200, 300):
        hist.add({"phases": {"lifting": d}})
    assert hist.typical("lifting") == 200 and hist.typical("raised") == 60
    again = OpeningHistory(tmp_path / "o.json", 30, hist.defaults)
    assert len(again.openings) == 3


# ---------------- autorecord with phases


class FakeRecorder:
    available = True

    def __init__(self):
        self.active_trigger, self.started, self.stopped, self.logged, self.hold_auto = None, [], 0, [], None

    def start(self, trigger, label, note):
        self.active_trigger = trigger
        self.started.append(trigger)

    def stop(self):
        self.active_trigger = None
        self.stopped += 1

    def log_detection(self, entry, at=None):
        self.logged.append(entry)


def st(phase, position="closed"):
    return {"bridge": {"position": position}, "boat": {"present": False}, "opening": {"phase": phase, "position": position},
            "far_leaf": {}, "traffic": {"northbound": {"level": "standstill", "flow": 0.0, "fill": 0.3}}, "deck": {"clear": True}}


def test_autorecord_starts_at_gates_down_and_stops_after_flowing():
    rec = FakeRecorder()
    auto = AutoRecorder(cfgmod.AutoRecordSection(post_roll_s=10), 1.0, rec)
    auto([], st("flowing"), now=0)
    assert rec.started == []
    auto([], st("gates_down"), now=1)
    assert rec.started == ["bridge"]
    auto([], st("lifting", "opening"), now=60)
    auto([], st("releasing"), now=200)
    auto([], st("flowing"), now=210)
    auto([], st("flowing"), now=221)
    assert rec.stopped == 1
    assert any("traffic" in e and e.get("phase") for e in rec.logged)


# ---------------- AIS


def test_distance_and_bearing():
    d, b = distance_bearing(BRIDGE[0], BRIDGE[1], BRIDGE[0], BRIDGE[1] + 0.01)
    assert 830 < d < 850 and 89 < b < 91  # 0.01° of longitude at 40.9° N, due east


def test_describe_approaching_from_the_east():
    v = describe({"lat": BRIDGE[0], "lon": BRIDGE[1] + 0.01, "sog_knots": 4.0, "cog_deg": 270.0},
                 BRIDGE[0], BRIDGE[1], 0.5)
    assert v["side"] == "east" and v["approaching"] and 380 < v["eta_s"] < 420
    away = describe({"lat": BRIDGE[0], "lon": BRIDGE[1] + 0.01, "sog_knots": 4.0, "cog_deg": 90.0}, BRIDGE[0], BRIDGE[1], 0.5)
    assert not away["approaching"] and away["eta_s"] is None


def test_aisstream_messages_fill_the_table():
    src = AisSource(cfgmod.AisSection(radius_m=2000), *BRIDGE)
    src.handle_aisstream({"MessageType": "PositionReport", "MetaData": {"MMSI": 367001, "ShipName": "TUG ONE "},
                          "Message": {"PositionReport": {"Latitude": BRIDGE[0], "Longitude": BRIDGE[1] - 0.012,
                                                         "Sog": 5.0, "Cog": 90.0, "TrueHeading": 511}}})
    src.handle_aisstream({"MessageType": "PositionReport", "MetaData": {"MMSI": 367002},
                          "Message": {"PositionReport": {"Latitude": BRIDGE[0] + 0.1, "Longitude": BRIDGE[1], "Sog": 1.0, "Cog": 0.0}}})
    src.handle_aisstream({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 367001},
                          "Message": {"ShipStaticData": {"Name": "TUG ONE", "Type": 52}}})
    snap = src.snapshot()
    assert [v["mmsi"] for v in snap["vessels"]] == [367001]  # the second is out of range
    v = snap["approaching"]
    assert v and v["side"] == "west" and v["name"] == "TUG ONE" and v["ship_type"] == 52 and v["heading_deg"] is None
    assert snap["messages"] == 2
    # Stale vessels drop out.
    assert src.snapshot(now=v["at"] + 1000)["vessels"] == []


def test_bbox_is_around_the_bridge():
    src = AisSource(cfgmod.AisSection(radius_m=1600), *BRIDGE)
    (lat1, lon1), (lat2, lon2) = src._bbox()
    assert lat1 < BRIDGE[0] < lat2 and lon1 < BRIDGE[1] < lon2
    assert 0.028 < lat2 - lat1 < 0.03


# ---------------- pipeline signals


def test_pipeline_builds_signals_from_lanes_and_leaves():
    cv2 = pytest.importorskip("cv2")
    from wth_edge.camera import HiRes
    from wth_edge.detectors import Pipeline

    cfg = cfgmod.from_dict({
        "boat_detector": {"enabled": False},
        "traffic_detector": {"standstill_s": 2, "hold_s": 1, "window_s": 2},
        "stages": {"enabled": False},  # every detector on every frame; test_stages covers the staged flow
        "site": {"rois": {"bridge": {"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.5},
                          "northbound": {"x": 0.0, "y": 0.8, "w": 0.2, "h": 0.2},
                          "southbound": {"x": 0.8, "y": 0.8, "w": 0.2, "h": 0.2},
                          "deck": {"x": 0.4, "y": 0.85, "w": 0.2, "h": 0.1}}},
    })
    pipe = Pipeline(cfg)
    h, w = 360, 640
    base = np.zeros((h, w, 3), np.uint8)
    cv2.line(base, (int(0.4 * w), int(0.7 * h)), (int(0.4 * w) + 150, int(0.7 * h) - 10), (255, 255, 255), 4)
    events = []
    for i in range(40):  # cars moving through both lanes: light traffic
        img = base.copy()
        x = int((i * 7) % 120)
        cv2.rectangle(img, (x, 300), (x + 20, 340), (200, 200, 200), -1)
        cv2.rectangle(img, (520 + x % 100, 300), (540 + x % 100, 340), (200, 200, 200), -1)
        events += pipe.process(i * 0.2, img, HiRes(img, "rgb", (w, h)))
    s = pipe.signals()
    assert s.leaf_angles.get("near") is not None and s.northbound in ("light", "moderate", "heavy")
    assert pipe.state()["opening"]["phase"] == "flowing"
    assert any(e["type"] == "traffic" for e in events) and any(e["type"] == "phase" for e in events)
    # A queue sitting still in both lanes.
    img = base.copy()
    cv2.rectangle(img, (0, 290), (128, 360), (200, 200, 200), -1)
    cv2.rectangle(img, (512, 290), (640, 360), (200, 200, 200), -1)
    for i in range(40, 200):
        events += pipe.process(i * 0.2, img, HiRes(img, "rgb", (w, h)))
    s = pipe.signals()
    assert s.northbound == "standstill" and s.southbound == "standstill" and s.deck_clear is True
    assert pipe.state()["opening"]["phase"] == "gates_down"
    assert pipe.state()["opening"]["website"]["northBoundTraffic"] == "standstill"
