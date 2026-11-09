import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from wth_edge import config as cfgmod  # noqa: E402
from wth_edge.autorecord import AutoRecorder  # noqa: E402
from wth_edge.camera import HiRes  # noqa: E402
from wth_edge.detectors import Pipeline  # noqa: E402
from wth_edge.detectors.boat import BoatTracker, Sighting  # noqa: E402
from wth_edge.detectors.bridge import BridgeTracker, measure_angle  # noqa: E402


def leaf(angle_deg, w=1280, h=720):
    """A dark frame with one bright 'bridge leaf' line inside the bridge ROI."""
    img = np.zeros((h, w), np.uint8)
    cx, cy, L = int(0.4 * w), int(0.7 * h), 150
    a = np.radians(angle_deg)
    cv2.line(img, (cx, cy), (int(cx + L * np.cos(a)), int(cy - L * np.sin(a))), 255, 4)
    return img


ROI = cfgmod.Roi(x=0.3, y=0.3, w=0.4, h=0.5)


@pytest.mark.parametrize("angle", [5, 30, 60, 80])
def test_measure_angle(angle):
    r = measure_angle(leaf(angle), ROI, cfgmod.BridgeDetectorSection())
    assert r is not None and abs(r.angle_deg - angle) < 3
    x1, y1, x2, y2 = r.line
    assert 0.3 <= min(x1, x2) and max(x1, x2) <= 0.7


def test_measure_angle_lores_frame():
    small = cv2.resize(leaf(60), (640, 360), interpolation=cv2.INTER_AREA)
    r = measure_angle(small, ROI, cfgmod.BridgeDetectorSection())
    assert r is not None and abs(r.angle_deg - 60) < 4


def run_bridge(angles, dt=0.2):
    cfg = cfgmod.BridgeDetectorSection()
    tr = BridgeTracker(cfg)
    seen = []
    t = 0.0
    for a in angles:
        reading = None if a is None else type("R", (), {"angle_deg": a, "line": (0, 0, 1, 1)})()
        if (p := tr.update(t, reading)) is not None:
            seen.append(p)
        t += dt
    return seen, tr


def test_bridge_tracker_full_cycle():
    up = list(np.linspace(5, 75, 60))
    angles = [5] * 20 + up + [75] * 20 + up[::-1] + [5] * 20
    seen, tr = run_bridge(angles)
    assert seen == ["closed", "opening", "open", "closing", "closed"]
    assert tr.state()["position"] == "closed" and tr.confidence == 1.0


def test_bridge_tracker_ignores_a_blip_and_times_out():
    seen, _ = run_bridge([5] * 20 + [70] + [5] * 20)
    assert seen == ["closed"]
    seen, tr = run_bridge([5] * 10 + [None] * 60)  # 12 s with no line
    assert seen == ["closed", "unknown"] and tr.state()["angle_deg"] is None


def test_boat_tracker_direction_and_end():
    cfg = cfgmod.BoatDetectorSection()
    tr = BoatTracker(cfg, "WEST", "EAST")
    events = []
    for i in range(10):  # moving right to left: east to west
        x = 0.8 - i * 0.01
        events += tr.update(i * 0.5, Sighting((x, 0.7, x + 0.05, 0.8), 0.5, "yolo"))
    assert [e["type"] for e in events] == ["boat_started", "boat_direction"]
    assert tr.present and tr.state()["direction"] == "e2w"
    assert tr.tick(10.0) == []
    end = tr.tick(5.0 + cfg.gone_s)
    assert end[0]["type"] == "boat_ended" and end[0]["direction"] == "e2w" and not tr.present


def test_motion_only_needs_travel():
    tr = BoatTracker(cfgmod.BoatDetectorSection(), require_travel=True)
    for i in range(10):  # ripples in one place
        tr.update(i * 0.5, Sighting((0.5, 0.7, 0.52, 0.72), 0.3, "motion"))
    assert not tr.present


class FakeRecorder:
    available = True

    def __init__(self):
        self.active_trigger = None
        self.started, self.stopped, self.logged = [], 0, []
        self.hold_auto = None

    def start(self, trigger, label, note):
        self.active_trigger = trigger
        self.started.append(trigger)

    def stop(self):
        self.active_trigger = None
        self.stopped += 1

    def log_detection(self, entry, at=None):
        self.logged.append(entry)


def st(position="closed", boat=False):
    return {"bridge": {"position": position}, "boat": {"present": boat}}


def test_autorecord_start_and_post_roll():
    rec = FakeRecorder()
    auto = AutoRecorder(cfgmod.AutoRecordSection(post_roll_s=10), 1.0, rec)
    auto([], st(), now=0)
    assert rec.started == []
    auto([{"type": "bridge"}], st("opening"), now=1)
    assert rec.started == ["bridge"]
    auto([], st("open", boat=True), now=2)
    auto([], st("closed"), now=30)
    auto([], st("closed"), now=35)
    assert rec.stopped == 0
    auto([], st("closed"), now=40.5)
    assert rec.stopped == 1
    assert any("bridge" in e for e in rec.logged)


def test_autorecord_leaves_manual_clips_alone():
    rec = FakeRecorder()
    rec.active_trigger = "manual"
    auto = AutoRecorder(cfgmod.AutoRecordSection(post_roll_s=1), 1.0, rec)
    auto([], st("closed"), now=0)
    auto([], st("closed"), now=100)
    assert rec.stopped == 0 and rec.started == []


def test_autorecord_waits_after_max_length():
    rec = FakeRecorder()
    auto = AutoRecorder(cfgmod.AutoRecordSection(), 1.0, rec)
    auto([], st("open"), now=0)
    rec.stop()
    rec.hold_auto = "x"  # the clip ran out while the bridge is still open
    auto([], st("open"), now=601)
    auto([], st("open"), now=602)
    assert rec.started == ["bridge"]
    auto([], st("closed"), now=700)
    auto([], st("opening"), now=800)
    assert rec.started == ["bridge", "bridge"]


def test_pipeline_bridge_from_hires_and_overlay_state():
    cfg = cfgmod.from_dict({
        "boat_detector": {"confirm": "motion"},
        "site": {"rois": {"bridge": vars(ROI), "boat": {"x": 0.0, "y": 0.85, "w": 1.0, "h": 0.15}}},
    })
    pipe = Pipeline(cfg)
    events = []
    for i in range(20):
        g = leaf(5)
        hi = HiRes(cv2.cvtColor(g, cv2.COLOR_GRAY2RGB), "rgb", (1280, 720))
        lores = cv2.resize(hi.rgb(), (640, 360))
        events += pipe.process(i * 0.2, lores, hi)
    # No lane boxes: nothing to gate on, so the pipeline watches the bridge straight away.
    assert events and events[0]["type"] == "stage" and events[0]["stage"] == "watch"
    bridge = [e for e in events if e["type"] == "bridge"]
    assert bridge and bridge[0]["position"] == "closed"
    s = pipe.state()
    assert s["bridge"]["line"] is not None and s["boat"]["present"] is False


def test_hires_yuv420():
    w, h = 64, 48
    yuv = np.zeros((h * 3 // 2, w), np.uint8)
    yuv[:h] = 200
    yuv[h:] = 128
    hi = HiRes(yuv, "yuv420", (w, h))
    assert hi.grey().shape == (h, w) and hi.grey()[0, 0] == 200
    assert hi.rgb().shape == (h, w, 3)


def test_eval_runs_on_a_store(tmp_path):
    av = pytest.importorskip("av")
    from wth_edge.clips import ClipStore
    from wth_edge.evaluate import run_eval

    store = ClipStore(tmp_path)
    clip_id = "20260403T000000Z_import_boat_test"
    p = store.paths(clip_id)
    p["video"].parent.mkdir(parents=True)
    with av.open(str(p["video"]), "w") as c:
        s = c.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = 1280, 720, "yuv420p"
        for _ in range(30):
            img = cv2.cvtColor(leaf(5), cv2.COLOR_GRAY2RGB)
            for pkt in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                c.mux(pkt)
        for pkt in s.encode():
            c.mux(pkt)
    store.write_meta({"id": clip_id, "label": "boat", "started_at": 1, "rois": {"bridge": vars(ROI)},
                      "import": {"direction": "e2w"}})
    cfg = cfgmod.from_dict({"boat_detector": {"confirm": "motion"}})
    out = tmp_path / "report.json"
    summary = run_eval(cfg, tmp_path, site=None, out=out)
    assert summary["boat_clips"] == 1 and summary["boat_found"] == 0
    assert '"closed"' in out.read_text()


def test_lane_watcher_boxes_cars():
    from wth_edge.config import Roi, TrafficDetectorSection
    from wth_edge.detectors.traffic import LaneWatcher

    w = LaneWatcher(TrafficDetectorSection())
    roi = Roi(0.5, 0.5, 0.25, 0.25)  # 80 x 45 px of a 320 x 180 frame
    road = np.full((180, 320), 90, dtype=np.uint8)
    assert w.update(road, roi) is None and w.vehicles == []
    w.update(road, roi)
    assert w.vehicles == []
    car = road.copy()
    car[100:110, 170:185] = 220  # one car, inside the lane box
    car[120:128, 210:220] = 20   # another
    w.update(car, roi)
    assert len(w.vehicles) == 2 and all(v.moving for v in w.vehicles)
    x1, y1, x2, y2 = w.vehicles[0].box
    assert abs(x1 - 170 / 320) < 0.02 and abs(y1 - 100 / 180) < 0.02 and abs(x2 - 185 / 320) < 0.02 and abs(y2 - 110 / 180) < 0.02
    w.update(car, roi)  # parked: still there, no longer moving
    assert len(w.vehicles) == 2 and not any(v.moving for v in w.vehicles)
