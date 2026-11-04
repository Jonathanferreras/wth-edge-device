import time

from wth_edge.display.screen import overall, render
from wth_edge.state import new_state, read_state, state_age_s, write_state


def test_state_roundtrip(tmp_path):
    path = tmp_path / "run" / "state.json"
    st = new_state("t1", "2.0")
    write_state(path, st)
    back = read_state(path)
    assert back["device_id"] == "t1"
    assert state_age_s(back) < 5
    assert list(path.parent.iterdir()) == [path]  # no temp files left behind


def test_read_state_tolerates_missing_and_corrupt(tmp_path):
    assert read_state(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_state(bad) is None


def test_overall_status():
    st = {"status": "ok"}
    assert overall(None, None, 15) == "waiting"
    assert overall(st, 60, 15) == "offline"
    assert overall(st, 1, 15) == "ok"


def test_render_every_state_is_240_square():
    ok = new_state("t", "2.0")
    ok.update(status="ok", camera={"ok": True, "fps": 14.9}, web_url="http://10.0.0.5:8080",
              telemetry={"cpu_temp_c": 52.3, "input_voltage_v": 5.1, "throttled": {"under_voltage_now": True}})
    for state, age in ((None, None), (ok, 1.0), (ok, 120.0), ({**ok, "camera": {"ok": False}}, 1.0)):
        img = render(state, age, 15, now=time.time())
        assert img.size == (240, 240)
