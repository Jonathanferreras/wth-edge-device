import numpy as np
import pytest

from wth_edge import config as cfgmod
from wth_edge.detectors.stages import StageInputs, StageMachine


def run(m, steps, dt=1.0, t0=0.0):
    """steps: list of (seconds, StageInputs). Returns stage events and the end time."""
    events, t = [], t0
    for secs, s in steps:
        for _ in range(int(secs / dt)):
            events += m.update(t, s)
            t += dt
    return events, t


MOVING = {"northbound": True, "southbound": True}
STOPPED = {"northbound": False, "southbound": False}


def cars(moving=True, leaves=None, boat=False):
    return StageInputs(dict(MOVING if moving else STOPPED), leaves, boat)


def machine(**over):
    return StageMachine(cfgmod.StagesSection(**over))


def test_flowing_traffic_stays_in_stage_one():
    m = machine()
    events, _ = run(m, [(120, cars())])
    assert events == [] and m.stage == "traffic" and not m.looks_at_leaves


def test_one_lane_still_moving_is_not_stopped():
    m = machine()
    events, _ = run(m, [(60, StageInputs({"northbound": False, "southbound": True}))])
    assert events == []


def test_stopped_traffic_moves_to_watch_after_15s():
    m = machine()
    events, _ = run(m, [(5, cars()), (20, cars(False))])
    assert [e["stage"] for e in events] == ["watch"]
    assert events[0]["at"] == pytest.approx(20) and events[0]["reason"] == "traffic_stopped"
    assert m.looks_at_leaves and m.looks_for_boats


def test_quiet_bridge_bounces_between_traffic_and_watch():
    m = machine()
    events, _ = run(m, [(5, cars()), (100, cars(False, leaves="flat"))])
    # 15 s to watch, 30 s quiet back to traffic, 15 s, 30 s...
    assert [e["stage"] for e in events] == ["watch", "traffic", "watch", "traffic"]
    assert [e["at"] for e in events] == [20, 50, 65, 95]
    assert events[1]["reason"] == "bridge_quiet"


def test_boat_keeps_the_watch_longer():
    m = machine(boat_quiet_s=120)
    events, t = run(m, [(5, cars()), (16, cars(False, "flat")), (10, cars(False, "flat", boat=True)),
                        (100, cars(False, "flat"))])
    assert [e["stage"] for e in events] == ["watch"]  # a plain watch would have ended at 50 s
    assert m.boat_seen
    events, _ = run(m, [(30, cars(False, "flat"))], t0=t)
    assert [e["stage"] for e in events] == ["traffic"]
    assert events[0]["at"] == pytest.approx(30 + 120)  # 120 s after the last sighting


def test_cars_moving_again_ends_the_watch():
    m = machine(resume_s=5)
    events, _ = run(m, [(5, cars()), (20, cars(False, "flat")), (10, cars(True, "flat"))])
    assert [(e["stage"], e["reason"]) for e in events] == [("watch", "traffic_stopped"), ("traffic", "traffic_moving")]
    assert events[1]["at"] == pytest.approx(25 + 5)


def test_full_opening_walks_all_three_stages():
    m = machine()
    events, _ = run(m, [
        (5, cars()),
        (20, cars(False, "flat", boat=True)),
        (120, cars(False, "active", boat=True)),   # lifting, up, lowering
        (10, cars(False, "flat")),                 # down: back to watching the bridge
        (10, cars(True, "flat")),                  # cars move: back to traffic
    ])
    assert [(e["stage"], e["reason"]) for e in events] == [
        ("watch", "traffic_stopped"), ("track", "leaves_moving"), ("watch", "bridge_lowered"), ("traffic", "traffic_moving")]


def test_track_holds_through_a_lost_leaf_then_gives_up():
    m = machine(lost_s=30)
    events, t = run(m, [(20, cars(False, "flat")), (5, cars(False, "active")), (20, cars(False, None))])
    assert m.stage == "track"
    events, _ = run(m, [(15, cars(False, None))], t0=t)
    assert [e["stage"] for e in events] == ["watch"] and events[0]["reason"] == "leaves_lost"


def test_no_lane_boxes_means_watch_the_bridge():
    m = machine()
    events, _ = run(m, [(120, StageInputs({}, "flat"))])
    assert [(e["stage"], e["reason"]) for e in events] == [("watch", "no_lanes")]


def test_unreadable_lanes_fall_back_to_watching_and_stay():
    m = machine()
    blind = StageInputs({"northbound": None, "southbound": None}, "flat")
    events, _ = run(m, [(200, blind)])
    assert [(e["stage"], e["reason"]) for e in events] == [("watch", "traffic_unreadable")]


# ---------------- in the pipeline


def test_pipeline_skips_leaves_and_boats_while_traffic_flows():
    cv2 = pytest.importorskip("cv2")
    from wth_edge.camera import HiRes
    from wth_edge.detectors import Pipeline

    cfg = cfgmod.from_dict({
        "boat_detector": {"confirm": "motion"},
        "traffic_detector": {"standstill_s": 2, "hold_s": 1, "window_s": 2},
        "stages": {"stopped_s": 3},
        "site": {"rois": {"bridge": {"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.5},
                          "boat": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.2},
                          "northbound": {"x": 0.0, "y": 0.8, "w": 0.2, "h": 0.2},
                          "southbound": {"x": 0.8, "y": 0.8, "w": 0.2, "h": 0.2}}},
    })
    pipe = Pipeline(cfg)
    h, w = 360, 640
    base = np.zeros((h, w, 3), np.uint8)
    cv2.line(base, (int(0.4 * w), int(0.7 * h)), (int(0.4 * w) + 150, int(0.7 * h) - 10), (255, 255, 255), 4)
    events = []
    for i in range(40):  # cars moving in both lanes
        img = base.copy()
        x = int((i * 7) % 120)
        cv2.rectangle(img, (x, 300), (x + 20, 340), (200, 200, 200), -1)
        cv2.rectangle(img, (520 + x % 100, 300), (540 + x % 100, 340), (200, 200, 200), -1)
        events += pipe.process(i * 0.2, img, HiRes(img, "rgb", (w, h)))
    st = pipe.state()
    assert st["stage"]["stage"] == "traffic"
    assert st["bridge"]["watching"] is False and st["bridge"]["angle_deg"] is None
    assert not any(e["type"] in ("bridge", "boat_started") for e in events)
    assert pipe.signals().leaf_angles == {}
    # A still queue: after the stop the leaves are measured.
    img = base.copy()
    cv2.rectangle(img, (0, 290), (128, 360), (200, 200, 200), -1)
    cv2.rectangle(img, (512, 290), (640, 360), (200, 200, 200), -1)
    for i in range(40, 200):
        events += pipe.process(i * 0.2, img, HiRes(img, "rgb", (w, h)))
    st = pipe.state()
    assert st["stage"]["stage"] == "watch" and st["stage"]["reason"] == "traffic_stopped"
    assert st["bridge"]["watching"] is True and st["bridge"]["position"] == "closed"
    assert any(e["type"] == "stage" for e in events) and any(e["type"] == "bridge" for e in events)


def test_stages_off_measures_everything():
    pytest.importorskip("cv2")
    from wth_edge.detectors import Pipeline

    cfg = cfgmod.from_dict({"stages": {"enabled": False}, "boat_detector": {"enabled": False}})
    pipe = Pipeline(cfg)
    assert pipe.stages is None and pipe.state()["stage"] is None
