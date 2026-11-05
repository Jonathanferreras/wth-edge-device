from wth_edge import config as cfgmod
from wth_edge.publishers import make_publisher
from wth_edge.publishers.firestore import FirestorePublisher, Timestamp, state_doc, to_value
from wth_edge.publishers.policy import BridgeStatePolicy


def site(position="closed", nb="light", sb="light", conf=0.8, wait=None):
    return {"position": position, "positionConfidence": conf, "northBoundTraffic": nb,
            "northBoundTrafficConfidence": conf, "southBoundTraffic": sb, "southBoundTrafficConfidence": conf,
            "estimatedWaitTime": wait}


# ---------------- policy


def test_first_state_and_positions_go_out_at_once():
    p = BridgeStatePolicy(traffic_hold_s=60, min_interval_s=30, heartbeat_s=600)
    assert p.offer(0, site()) is not None
    assert p.offer(1, site()) is None
    assert p.offer(2, site("opening", "standstill", "standstill"))["position"] == "opening"


def test_confidence_jitter_never_writes():
    p = BridgeStatePolicy()
    p.offer(0, site(conf=0.8))
    assert all(p.offer(t, site(conf=0.5 + t / 1000)) is None for t in range(1, 300))


def test_traffic_level_must_hold_before_it_goes_out():
    p = BridgeStatePolicy(traffic_hold_s=60, min_interval_s=30, heartbeat_s=600)
    p.offer(0, site(nb="light"))
    # Flickers between moderate and light: never published.
    for t in range(1, 200):
        assert p.offer(t, site(nb="moderate" if t % 20 < 10 else "light")) is None
    # Holds moderate: published 60 s after it started.
    assert p.offer(200, site(nb="moderate")) is None
    assert p.offer(259, site(nb="moderate")) is None
    assert p.offer(260, site(nb="moderate"))["northBoundTraffic"] == "moderate"


def test_heartbeat_rewrites_unchanged_state():
    p = BridgeStatePolicy(heartbeat_s=600)
    p.offer(0, site())
    assert p.offer(599, site()) is None
    assert p.offer(600, site()) is not None


def test_withhold_sends_unknown():
    p = BridgeStatePolicy(withhold=("traffic", "wait"))
    out = p.offer(0, site("open", "standstill", "standstill", wait={"status": "estimated", "startedAt": 1.0}))
    assert out["northBoundTraffic"] == out["southBoundTraffic"] == "unknown"
    assert out["estimatedWaitTime"] is None
    assert out["position"] == "open"


# ---------------- firestore


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, accepts=None, fail=False):
        self.accepts, self.fail = accepts, fail
        self.commits, self.gets = [], 0

    def get(self, url, params=None, timeout=None):
        self.gets += 1
        if self.accepts is None:
            return Resp(404)
        return Resp(200, {"fields": {"acceptsDeviceUpdates": {"booleanValue": self.accepts}}})

    def post(self, url, json=None, timeout=None):
        if self.fail:
            raise ConnectionError("offline")
        self.commits.append(json["writes"])
        return Resp(200, {})


def pub(session, **over):
    cfg = cfgmod.PublisherSection(driver="firestore", project_id="wth-test", **over)
    return FirestorePublisher(cfg, session=session, start=False)


def names(writes):
    return [w["update"]["name"].split("/documents/")[1].split("/")[0] for w in writes]


def test_state_doc_matches_web_shape():
    wait = {"status": "estimated", "startedAt": 1_700_000_000.0, "initialTotalMinutes": 6.0,
            "estimatedTotalMinutes": 7.5, "remainingMinutes": 3.0, "revisionReason": "device_update",
            "confidence": 0.6}
    doc = state_doc("hutch-01", site("open", "standstill", "standstill", wait=wait), 1_700_000_100.0)
    assert doc["sourceType"] == "device" and doc["sourceId"] == "hutch-01" and doc["id"] == "current"
    assert "acceptsDeviceUpdates" not in doc
    w = doc["estimatedWaitTime"]
    assert "remainingMinutes" not in w
    assert to_value(w["startedAt"]) == {"timestampValue": "2023-11-14T22:13:20Z"}
    assert isinstance(w["lastRevisedAt"], Timestamp)


def test_writes_state_with_history_event_and_mask():
    s = FakeSession()
    p = pub(s)
    p.publish_bridge_state("hutch-01", site())
    p.flush(100)
    writes = s.commits[0]
    assert names(writes) == ["bridgeState", "events"]
    assert "acceptsDeviceUpdates" not in writes[0]["updateMask"]["fieldPaths"]
    assert writes[0]["updateTransforms"][0]["fieldPath"] == "updatedAt"
    # A heartbeat of the same state: no second history event.
    p.publish_bridge_state("hutch-01", site(conf=0.7))
    p.flush(200)
    assert names(s.commits[1]) == ["bridgeState"]


def test_admin_override_holds_state_until_handed_back():
    s = FakeSession(accepts=False)
    p = pub(s, override_check_s=60)
    p.publish_bridge_state("hutch-01", site())
    p.publish_event("hutch-01", {"type": "phase", "at": 50.0, "phase": "gates_down"})
    p.flush(100)
    assert names(s.commits[0]) == ["deviceEvents"]
    assert p.held_by_admin
    p.flush(120)  # not time to check again yet
    assert s.gets == 1
    s.accepts = True
    p.flush(161)
    assert names(s.commits[-1])[0] == "bridgeState"
    assert not p.held_by_admin


def test_noisy_events_are_not_sent():
    s = FakeSession()
    p = pub(s)
    p.publish_event("hutch-01", {"type": "traffic", "at": 1.0, "level": "heavy"})
    p.flush(10)
    assert s.commits == []


def test_failure_backs_off_and_keeps_data():
    s = FakeSession(fail=True)
    p = pub(s)
    p.publish_telemetry("hutch-01", {"cpu_temp_c": 50.0, "ip": ["10.0.0.2"]})
    p.flush(100)
    assert p.ok is False and p.status()["error"]
    s.fail = False
    p.flush(101)  # still backing off
    assert s.commits == []
    p.flush(106)
    assert names(s.commits[0]) == ["devices"]
    assert p.ok


def test_make_publisher_firestore():
    cfg = cfgmod.PublisherSection(driver="firestore", project_id="x", credentials_file="/nonexistent.json")
    p = make_publisher(cfg)
    try:
        assert p.name == "firestore" and p.project == "x"
    finally:
        p.close()
