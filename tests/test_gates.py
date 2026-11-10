"""The barrier arms: the detector, the tracker and how the opening and stage machines use them."""

import numpy as np
import pytest

pytest.importorskip("cv2")
import cv2
from test_opening import flowing, machine, run, stopped

from wth_edge import config as cfgmod
from wth_edge.detectors.gate import (
    GateTracker,
    measure_gate,
    summary,
)
from wth_edge.detectors.stages import StageInputs, StageMachine
from wth_edge.opening import Signals

ROI = cfgmod.Roi(x=0.40, y=0.30, w=0.03, h=0.30)
ORANGE, TREES = (230, 160, 40), (150, 90, 120)  # the arm, and the pink NoIR trees behind it


PINK = (235, 170, 185)  # the arm in low sun on the NoIR camera: bright, but not orange


def arm(angle: float | None, w=1280, h=720, light=1.0, colour=ORANGE) -> np.ndarray:
    """An RGB scene with trees behind, a post, and an orange arm at ``angle`` (90 = up,
    None = no arm) pivoting on the bottom middle of ROI."""
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = TREES
    rng = np.random.default_rng(0)
    for _ in range(60):  # tree trunks: vertical edges that fooled the edge detector
        x = int(rng.integers(0, w))
        cv2.line(img, (x, 0), (x, h), (60, 40, 50), 2)
    x1, y1, x2, y2 = ROI.to_pixels(w, h)
    px, py, length = (x1 + x2) // 2, y2, y2 - y1
    cv2.rectangle(img, (px - 4, py - 12), (px + 4, py + 10), ORANGE, -1)  # the housing
    if angle is not None:
        tip = (int(px + np.cos(np.radians(angle)) * length), int(py - np.sin(np.radians(angle)) * length))
        cv2.line(img, (px, py), tip, colour, 5)
    return (img.astype(np.float32) * light).astype(np.uint8)


@pytest.mark.parametrize("angle,want", [(90, "up"), (0, "down"), (180, "down"), (None, "down")])
def test_measure_gate(angle, want):
    cfg = cfgmod.GateDetectorSection()
    r = measure_gate(arm(angle), ROI, cfg)
    assert r.position(cfg) == want and not r.dark


def test_measure_gate_low_sun_arm_is_still_up():
    """Dusk on 2026-11-10: the arms' hue slid out of the mask and three of four read down."""
    cfg = cfgmod.GateDetectorSection()
    r = measure_gate(arm(90, colour=PINK), ROI, cfg)
    assert r.share < cfg.down_max and r.contrast > cfg.contrast_up_min
    assert r.position(cfg) == "up"
    gone = measure_gate(arm(None), ROI, cfg)
    assert gone.contrast < cfg.contrast_down_max and gone.position(cfg) == "down"


@pytest.mark.parametrize("angle", [90, 60, 30, 135])
def test_measure_gate_follows_the_swing(angle):
    r = measure_gate(arm(angle), ROI, cfgmod.GateDetectorSection())
    assert r.angle_deg == pytest.approx(angle, abs=5)


def test_measure_gate_lores_dark_and_grey():
    cfg = cfgmod.GateDetectorSection()
    small = cv2.resize(arm(90), (640, 360), interpolation=cv2.INTER_AREA)
    assert measure_gate(small, ROI, cfg).position(cfg) == "up"
    night = measure_gate(arm(90, light=0.15), ROI, cfg)
    assert night.dark and night.position(cfg) is None
    assert measure_gate(cv2.cvtColor(arm(90), cv2.COLOR_RGB2GRAY), ROI, cfg) is None


def feed(tr, t0, t1, angle, fps=2):
    cfg = cfgmod.GateDetectorSection()
    out = []
    frame = arm(angle)
    for i in range(int((t1 - t0) * fps)):
        t = t0 + i / fps
        if (c := tr.update(t, measure_gate(frame, ROI, cfg))):
            out.append((t, c))
    return out


def test_gate_tracker_follows_the_arm_down_and_up():
    tr = GateTracker(cfgmod.GateDetectorSection())
    changes = feed(tr, 0, 20, 90)
    st = tr.state()
    assert st["angle_deg"] == 90 and st["line"][1] > st["line"][3]  # the tip is above the base
    changes += feed(tr, 20, 40, None)
    changes += feed(tr, 40, 50, 90)
    assert [c for _, c in changes] == ["up", "down", "up"]
    assert 20 < changes[1][0] <= 24


def test_gate_tracker_unknown_in_the_dark():
    cfg = cfgmod.GateDetectorSection()
    tr = GateTracker(cfg)
    night = measure_gate(arm(90, light=0.15), ROI, cfg)
    for t in range(30):
        tr.update(float(t), night)
    assert tr.position == "unknown"


def test_summary():
    assert summary([]) is None and summary([("unknown", 0.0)]) is None
    assert summary([("up", 1.0), ("up", 0.9), ("down", 1.0), ("unknown", 0.0)]) == "up"
    assert summary([("unknown", 0), ("down", 1.0), ("down", 1.0), ("up", 0.6)]) == "down"
    assert summary([("up", 0.5), ("down", 1.0)]) == "down"  # the steadier arm wins
    assert summary([("up", 1.0), ("down", 1.0)]) is None


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
        events += p.process(float(t), arm(90))
    assert any(e["type"] == "gate" and e["position"] == "up" for e in events)
    st = p.state()["gates"]
    assert st["summary"] == "up" and st["arms"]["gate_1"]["position"] == "up"
    assert p.signals().gates == "up"


def test_swing_side_ignores_a_neighbour_on_the_other_side():
    cfg = cfgmod.GateDetectorSection()
    frame = arm(150)  # the arm leans left...
    assert measure_gate(frame, ROI, cfg).angle_deg == pytest.approx(150, abs=5)
    assert measure_gate(frame, ROI, cfg, side="right").angle_deg is None  # ...but this arm swings right


def test_lowered_arm_stops_being_drawn():
    cfg = cfgmod.GateDetectorSection()
    tr = GateTracker(cfg)
    feed(tr, 0, 10, 90)
    feed(tr, 10, 14, 10)
    assert tr.position == "down" and tr.state()["line"] is not None
    feed(tr, 14, 40, 10)
    assert tr.state()["line"] is None
