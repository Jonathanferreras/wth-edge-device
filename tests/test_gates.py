"""The barrier arms: the detector, the tracker and how the opening and stage machines use them."""

import numpy as np
import pytest

pytest.importorskip("cv2")
import cv2
from test_opening import flowing, machine, run, stopped

from wth_edge import config as cfgmod
from wth_edge.detectors.gate import (
    GateReading,
    GateTracker,
    measure_gate,
    summary,
)
from wth_edge.detectors.stages import StageInputs, StageMachine
from wth_edge.opening import Signals

ROI = cfgmod.Roi(x=0.40, y=0.30, w=0.03, h=0.30)


def arm(up: bool | None, w=1280, h=720) -> np.ndarray:
    """A grey scene with a striped barrier arm standing up in ROI, or lying across it."""
    img = np.full((h, w), 90, np.uint8)
    x1, y1, x2, y2 = ROI.to_pixels(w, h)
    cx = (x1 + x2) // 2
    if up:
        cv2.line(img, (cx, y2 - 2), (cx, y1 + 2), 235, 4)
    elif up is False:
        cv2.line(img, (cx - 200, y2 - 12), (cx + 30, y2 - 12), 235, 4)
        cv2.line(img, (cx, y2 - 2), (cx, y2 - 14), 235, 4)  # the post
    return img


@pytest.mark.parametrize("up,want", [(True, True), (False, False), (None, False)])
def test_measure_gate(up, want):
    r = measure_gate(arm(up), ROI, cfgmod.GateDetectorSection())
    assert r.up is want and not r.dark


def test_measure_gate_lores_and_dim():
    cfg = cfgmod.GateDetectorSection()
    small = cv2.resize(arm(True), (640, 360), interpolation=cv2.INTER_AREA)
    assert measure_gate(small, ROI, cfg).up
    dusk = (arm(True).astype(np.float32) * 0.25).astype(np.uint8)  # the contrast stretch finds it
    r = measure_gate(dusk, ROI, cfg)
    assert r.up and r.dark


def feed(tr, t0, t1, up, dark=False, fps=2):
    out = []
    for i in range(int((t1 - t0) * fps)):
        t = t0 + i / fps
        if (c := tr.update(t, GateReading(up(t) if callable(up) else up, dark))):
            out.append((t, c))
    return out


def test_gate_down_when_a_well_seen_arm_goes_missing():
    tr = GateTracker(cfgmod.GateDetectorSection())
    changes = feed(tr, 0, 200, True)
    changes += feed(tr, 200, 300, False)
    changes += feed(tr, 300, 310, True)
    assert [c for _, c in changes] == ["up", "down", "up"]
    assert changes[1][0] == pytest.approx(208, abs=1)


def test_gate_rarely_seen_or_dark_is_unknown_not_down():
    cfg = cfgmod.GateDetectorSection()
    tr = GateTracker(cfg)
    feed(tr, 0, 200, lambda t: int(t) % 5 == 0)  # seen one second in five: not trusted
    feed(tr, 200, 260, False)
    assert tr.position == "unknown"
    tr = GateTracker(cfg)
    feed(tr, 0, 200, True)
    feed(tr, 200, 260, False, dark=True)
    assert tr.position == "unknown"


def test_summary():
    assert summary([]) is None and summary(["unknown"]) is None
    assert summary(["up", "up", "down", "unknown"]) == "up"
    assert summary(["unknown", "down", "down", "up"]) == "down"
    assert summary(["up", "down"]) is None


def gated(s: Signals, gates: str | None) -> Signals:
    s.gates = gates
    return s


def test_arms_down_with_traffic_stopped_starts_gates_down_fast():
    m = machine()
    events, _ = run(m, [(5, flowing()), (10, gated(stopped(), "down"))])
    assert [e["phase"] for e in events] == ["flowing", "gates_down"]
    assert events[1]["at"] - 5 <= 4
    assert m.confidence == 0.8


def test_arms_down_holds_gates_down_past_the_false_alarm():
    m = machine(false_alarm_s=60)
    events, _ = run(m, [(5, flowing()), (200, gated(stopped(), "down"))])
    assert [e["phase"] for e in events] == ["flowing", "gates_down"]


def test_stopped_traffic_with_arms_up_is_not_an_opening():
    m = machine()
    events, _ = run(m, [(5, flowing()), (120, gated(stopped(), "up"))])
    assert [e["phase"] for e in events] == ["flowing"]


def test_arms_up_again_ends_gates_down():
    m = machine()
    events, _ = run(m, [(5, flowing()), (10, gated(stopped(), "down")), (20, gated(stopped(), "up"))])
    assert [e["phase"] for e in events] == ["flowing", "gates_down", "flowing"]


def test_leaf_reading_with_arms_up_is_not_an_opening():
    """The overnight false openings: a lamp post in the far leaf box, empty lanes, arms up."""
    m = machine()
    night = Signals(northbound="light", southbound="light", northbound_moving=False, southbound_moving=False,
                    leaf_angles={"near": 3, "far": 90}, leaf_positions={"near": "closed", "far": "open"}, gates="up")
    events, _ = run(m, [(5, flowing()), (120, night)])
    assert [e["phase"] for e in events] == ["flowing"]


def test_arms_up_during_a_lift_ends_it():
    m = machine()
    s = Signals(northbound="standstill", southbound="standstill", leaf_angles={"near": 3, "far": 40},
                leaf_rates={"far": 2.0})
    events, _ = run(m, [(5, flowing()), (10, s), (15, gated(stopped(), "up"))])
    assert [e["phase"] for e in events] == ["flowing", "lifting", "releasing"]


def test_stages_watch_while_the_arms_are_down():
    m = StageMachine(cfgmod.StagesSection())
    moving = {"northbound": True, "southbound": True}
    events = []
    for t in range(200):
        events += m.update(float(t), StageInputs(dict(moving), None, False, "down" if t >= 10 else "up"))
    assert [(e["stage"], e["reason"]) for e in events] == [("watch", "gates_down")]
    assert events[0]["at"] == 10 and m.stage == "watch"


def test_pipeline_reads_gate_boxes(tmp_path):
    from wth_edge.detectors import Pipeline

    cfg = cfgmod.Config()
    cfg.stages.enabled = False
    rois = {"gate_1": ROI}
    p = Pipeline(cfg, rois)
    events = []
    for t in range(3):
        events += p.process(float(t), np.dstack([arm(True)] * 3))
    assert any(e["type"] == "gate" and e["position"] == "up" for e in events)
    st = p.state()["gates"]
    assert st["summary"] == "up" and st["arms"]["gate_1"]["position"] == "up"
    assert p.signals().gates == "up"
