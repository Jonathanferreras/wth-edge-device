import json

import pytest

from wth_edge import config as cfgmod
from wth_edge import notify
from wth_edge.notify import AlertRules, Notifier


def rules(**over):
    over.setdefault("max_per_hour", 0)
    return AlertRules(cfgmod.NotifySection(enabled=True, **over))


def phase(at, ph, prev, boat=None):
    return {"type": "phase", "at": at, "phase": ph, "previous": prev, "boat_direction": boat}


def kinds(alerts):
    return [a.kind for a in alerts]


def vessel(mmsi=111, eta=300, name="TUG ONE", dist=900):
    return {"ais": {"approaching": {"mmsi": mmsi, "name": name, "side": "east", "distance_m": dist, "eta_s": eta}}}


def test_full_opening_gives_three_alerts():
    r = rules()
    st = {"opening": {"wait": {"initialTotalMinutes": 6.8, "remainingMinutes": 4.2}}}
    out = r.check([phase(0, "gates_down", "flowing")], st, 0)
    out += r.check([phase(60, "lifting", "gates_down", "e2w")], st, 60)
    out += r.check([phase(120, "raised", "lifting")], st, 120)
    out += r.check([phase(180, "lowering", "raised")], st, 180)
    out += r.check([phase(240, "releasing", "lowering")], st, 240)
    out += r.check([phase(300, "flowing", "releasing")], {}, 300)
    assert kinds(out) == ["gates_down", "opening", "closed"]
    assert "about 7 min" in out[0].message
    assert "heading west" in out[1].message and "about 4 min" in out[1].message
    assert "about 5 min" in out[2].message


def test_false_alarm_at_the_gates_ends_quietly():
    r = rules()
    out = r.check([phase(0, "gates_down", "flowing")], {}, 0)
    out += r.check([phase(180, "flowing", "gates_down")], {}, 180)
    assert kinds(out) == ["gates_down"]


def test_leaf_bouncing_is_one_opening_alert():
    r = rules(cooldown_s=0)
    out = r.check([phase(0, "lifting", "flowing")], {}, 0)
    out += r.check([phase(30, "lowering", "lifting")], {}, 30)
    out += r.check([phase(40, "lifting", "lowering")], {}, 40)
    assert kinds(out) == ["opening"]


def test_cooldown_spans_back_to_back_openings():
    r = rules(cooldown_s=900)
    out = []
    for t0 in (0, 400):
        out += r.check([phase(t0, "lifting", "flowing")], {}, t0)
        out += r.check([phase(t0 + 100, "flowing", "releasing")], {}, t0 + 100)
    assert kinds(out) == ["opening", "closed"]
    out = r.check([phase(2000, "lifting", "flowing")], {}, 2000)
    assert kinds(out) == ["opening"]


def test_boat_seen_alone_and_folded_into_an_opening():
    r = rules(cooldown_s=0)
    out = r.check([{"type": "boat_started", "at": 0, "direction": "w2e"}], {}, 0)
    assert kinds(out) == ["boat_seen"] and "heading east" in out[0].message
    r.check([phase(10, "lifting", "flowing")], {}, 10)
    assert r.check([{"type": "boat_started", "at": 20, "direction": "e2w"}], {}, 20) == []


def test_ais_vessel_announced_once_and_only_when_close():
    r = rules(cooldown_s=0, ais_max_eta_s=600)
    assert r.check([], vessel(eta=1200), 0) == []
    out = r.check([], vessel(eta=500), 10)
    assert kinds(out) == ["boat_approaching"]
    assert out[0].message == "TUG ONE is 0.6 mi east of the bridge and closing, about 8 min out."
    assert r.check([], vessel(eta=400), 20) == []         # same boat
    assert kinds(r.check([], vessel(mmsi=222, eta=300), 30)) == ["boat_approaching"]
    assert kinds(r.check([], vessel(eta=300), 4000)) == ["boat_approaching"]  # back an hour later


def test_unwanted_kinds_quiet_hours_and_hourly_cap():
    r = rules(on=("opening",), cooldown_s=0)
    assert r.check([phase(0, "gates_down", "flowing")], {}, 0) == []
    assert kinds(r.check([phase(5, "lifting", "gates_down")], {}, 5)) == ["opening"]

    r = rules(quiet_hours=(22, 6))
    assert r.check([phase(0, "lifting", "flowing")], {}, 0, hour=23) == []
    assert r.check([phase(0, "lifting", "flowing")], {}, 0, hour=3) == []
    r = rules(quiet_hours=(22, 6))
    assert kinds(r.check([phase(0, "lifting", "flowing")], {}, 0, hour=12)) == ["opening"]

    r = rules(cooldown_s=0, max_per_hour=2)
    sent = [r.check([{"type": "boat_started", "direction": None}], {}, t) for t in (0, 10, 20)]
    assert [len(s) for s in sent] == [1, 1, 0]
    assert len(r.check([{"type": "boat_started", "direction": None}], {}, 3700)) == 1


# ---------------- sending


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def test_ntfy_posts_json_with_topic_and_token(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["body"], seen["headers"] = req.full_url, json.loads(req.data), dict(req.header_items())
        return FakeResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("WTH_NTFY_TOPIC", "wth-secret")
    monkeypatch.setenv("WTH_NTFY_TOKEN", "tk")
    cfg = cfgmod.NotifySection(enabled=True, click_url="https://example.org")
    res = Notifier(cfg).deliver(notify.Alert("opening", "Bridge opening", "Up it goes.", 0, ["ship"]))
    assert res == {"ntfy": None}
    assert seen["url"] == "https://ntfy.sh"
    assert seen["body"] == {"topic": "wth-secret", "title": "Bridge opening", "message": "Up it goes.",
                            "tags": ["ship"], "priority": 4, "click": "https://example.org"}
    assert seen["headers"]["Authorization"] == "Bearer tk"


def test_missing_secret_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("WTH_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("WTH_TELEGRAM_CHAT_ID", raising=False)
    n = Notifier(cfgmod.NotifySection(enabled=True, channels=("telegram", "console")))
    res = n.deliver(notify.sample_alert())
    assert res["console"] is None and "WTH_TELEGRAM_TOKEN" in res["telegram"]
    assert n.sent == 1 and "telegram" in n.status()["errors"]
    assert notify.missing_secrets("telegram") == ["WTH_TELEGRAM_TOKEN", "WTH_TELEGRAM_CHAT_ID"]


def test_listener_sends_on_its_own_thread(monkeypatch):
    got = []
    monkeypatch.setattr(notify, "send", lambda ch, alert, cfg: got.append((ch, alert.kind)))
    n = Notifier(cfgmod.NotifySection(enabled=True, channels=("console",)))
    n.start()
    n([phase(0, "lifting", "flowing")], {}, now=0)
    n.stop()
    assert got == [("console", "opening")]


def test_config_reads_notify_section():
    cfg = cfgmod.from_dict({"notify": {"enabled": True, "channels": ["ntfy", "email"], "quiet_hours": [23, 6]}})
    assert cfg.notify.channels == ("ntfy", "email") and cfg.notify.quiet_hours == (23, 6)
