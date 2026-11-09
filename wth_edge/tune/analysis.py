"""Compare, fit, replay: the tuning part of the loop.

* ``score``: for each reviewed moment, did the detector's reading (from the clip's detection
  log, or from a replay) match what the review saw? A reading counts as matching if any
  logged second from the moment to a few seconds after it agrees, so the detector's own
  hold times don't count against it.
* ``fit``: for each tunable threshold, the value that best separates the reviewed classes
  using the signal the detector logged at that moment (lane flow and fill, deck fill, leaf
  angle). Changes are limited per round so one odd hour can't swing a value far.
* ``replay``: run the recorded chunks through ``Pipeline`` with the old and the new settings
  and score both against the same review, so a change is only kept when it helps.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from .reference import LANES, live_summary, readings

log = logging.getLogger(__name__)

FIELDS = ("northbound", "southbound", "deck", "bridge", "boat")
MOVING_LEVELS = ("light", "moderate", "heavy")
# Seconds after the reviewed moment in which a matching reading still counts.
LAG_S = 5.0


# ---------------------------------------------------------------- matching
def lane_ok(label: dict | None, level: str | None) -> bool | None:
    motion = (label or {}).get("motion", "unclear")
    if motion == "unclear":
        return None
    if motion == "empty":
        return level == "light"
    if motion == "moving":
        return level in MOVING_LEVELS
    return level == "standstill"


def amount_ok(label: dict | None, level: str | None) -> bool | None:
    label = label or {}
    if label.get("motion") != "moving" or label.get("amount") not in MOVING_LEVELS:
        return None
    return level == label["amount"]


def field_ok(name: str, label: dict[str, Any], live: dict[str, Any]) -> bool | None:
    if name in LANES:
        if name not in live:
            return None
        return lane_ok(label.get(name), live.get(name))
    seen = label.get(name, "unclear")
    if seen == "unclear":
        return None
    if name == "deck":
        if "deck" not in live:
            return None
        return live["deck"] is (seen == "clear")
    if name == "bridge":
        if "bridge" not in live:
            return None
        return live["bridge"] == ("closed" if seen == "down" else "raised")
    if name == "boat":
        return live.get("boat") is (seen == "yes")
    return None


def window(entries: list[dict[str, Any]], t0: float, t1: float) -> list[dict[str, Any]]:
    return [e for e in entries if t0 - 0.6 <= e["t"] <= t1 + 0.6]


def nearest(entries: list[dict[str, Any]], t: float, tol: float = 1.5) -> dict[str, Any] | None:
    best = min(entries, key=lambda e: abs(e["t"] - t), default=None)
    return best if best is not None and abs(best["t"] - t) <= tol else None


def judge(sample: dict[str, Any], entries: list[dict[str, Any]], gap: float,
          fields: Iterable[str] = FIELDS) -> dict[str, Any]:
    """Per field: ok (bool), skipped (None), plus what the detector said at the moment."""
    label = sample.get("label") or {}
    t = sample["t"]
    near = live_summary(nearest(entries, t + gap))
    span = [live_summary(e) for e in window(entries, t, t + gap + LAG_S)]
    out: dict[str, Any] = {"live": near, "ok": {}}
    for name in fields:
        # None for every second: not reviewed ("unclear") or not measured here (no such box).
        results = [field_ok(name, label, s) for s in span]
        known = [r for r in results if r is not None]
        out["ok"][name] = any(known) if known else None
    for lane in LANES:
        if lane in fields:
            results = [amount_ok(label.get(lane), s.get(lane)) for s in span]
            known = [r for r in results if r is not None]
            out["ok"][f"{lane}_amount"] = (any(known) if known else None)
    return out


def score(samples: list[dict[str, Any]], logs: dict[str, list[dict[str, Any]]], gap: float,
          fields: Iterable[str] = FIELDS) -> dict[str, Any]:
    """Accuracy per field and overall for the samples that have a review."""
    fields = tuple(fields)
    per: dict[str, dict[str, int]] = {}
    for s in samples:
        if not s.get("label"):
            continue
        j = judge(s, logs.get(s["clip_id"], []), gap, fields)
        for name, ok in j["ok"].items():
            if ok is None:
                continue
            p = per.setdefault(name, {"n": 0, "ok": 0})
            p["n"] += 1
            p["ok"] += int(ok)
    for p in per.values():
        p["accuracy"] = round(p["ok"] / p["n"], 3) if p["n"] else None
    main = [per[f] for f in fields if f in per]
    n = sum(p["n"] for p in main)
    ok = sum(p["ok"] for p in main)
    return {"fields": per, "n": n, "ok": ok, "accuracy": round(ok / n, 3) if n else None}


# ---------------------------------------------------------------- fitting
def best_threshold(pos: list[float], neg: list[float], current: float, min_each: int = 3) -> float | None:
    """The cut that best separates ``pos`` (should be >= cut) from ``neg`` (< cut), by
    balanced accuracy; ties go to the cut closest to ``current``. None without enough data."""
    if len(pos) < min_each or len(neg) < min_each:
        return None
    values = sorted(set(pos) | set(neg))
    cuts = [(a + b) / 2 for a, b in zip(values, values[1:])] or [current]

    def bal(c: float) -> float:
        return (sum(v >= c for v in pos) / len(pos) + sum(v < c for v in neg) / len(neg)) / 2

    base = bal(current)
    best = max(cuts, key=lambda c: (round(bal(c), 6), -abs(c - current)))
    return best if bal(best) > base + 1e-9 else None


def _limit(new: float, cur: float, lo: float = 0.5, hi: float = 2.0) -> float:
    if cur > 0:
        new = min(max(new, cur * lo), cur * hi)
    return new


def _signals(sample: dict[str, Any], entries: list[dict[str, Any]], gap: float) -> dict[str, Any] | None:
    return nearest(entries, sample["t"] + gap)


def fit(samples: list[dict[str, Any]], logs: dict[str, list[dict[str, Any]]], config, gap: float) -> list[dict[str, Any]]:
    """Proposed changes: [{key, old, new, why, evidence}]."""
    tcfg, bcfg = config.traffic_detector, config.bridge_detector
    flow_pos, flow_neg, fill_pos, fill_neg = [], [], [], []
    mod_pos, mod_neg, heavy_pos, heavy_neg = [], [], [], []
    deck_pos, deck_neg, ang_pos, ang_neg = [], [], [], []
    for s in samples:
        label = s.get("label")
        if not label:
            continue
        e = _signals(s, logs.get(s["clip_id"], []), gap)
        if not e:
            continue
        for lane in LANES:
            lab = label.get(lane) or {}
            sig = (e.get("traffic") or {}).get(lane) or {}
            flow, fill = sig.get("flow"), sig.get("fill")
            motion, amount = lab.get("motion"), lab.get("amount")
            if flow is not None:
                if motion == "moving":
                    flow_pos.append(flow)
                elif motion == "stopped":
                    flow_neg.append(flow)
            if fill is not None:
                if motion in ("moving", "stopped"):
                    fill_pos.append(fill)
                elif motion == "empty":
                    fill_neg.append(fill)
                if motion == "moving" and amount in MOVING_LEVELS:
                    (mod_pos if amount in ("moderate", "heavy") else mod_neg).append(fill)
                    (heavy_pos if amount == "heavy" else heavy_neg).append(fill)
        deck = e.get("deck") or {}
        if deck.get("fill") is not None and label.get("deck") in ("clear", "cars"):
            (deck_pos if label["deck"] == "cars" else deck_neg).append(deck["fill"])
        for leaf in ("bridge", "far_leaf"):
            ang = (e.get(leaf) or {}).get("angle_deg")
            if ang is not None and label.get("bridge") in ("down", "raised"):
                (ang_pos if label["bridge"] == "raised" else ang_neg).append(ang)

    changes: list[dict[str, Any]] = []

    def propose(key: str, cur: float, pos: list[float], neg: list[float], why: str, digits: int = 4) -> float:
        cut = best_threshold(pos, neg, cur)
        if cut is None:
            return cur
        new = round(_limit(cut, cur), digits)
        if cur and abs(new - cur) / abs(cur) < 0.05:
            return cur
        changes.append({"key": key, "old": cur, "new": new, "why": why,
                        "evidence": {"above": len(pos), "below": len(neg)}})
        return new

    propose("traffic_detector.flow_min", tcfg.flow_min, flow_pos, flow_neg,
            "how much change in a lane box counts as cars moving (moving vs stopped lanes)")
    present = propose("traffic_detector.present_min", tcfg.present_min, fill_pos, fill_neg,
                      "how much of a lane must differ from the empty road to count as cars there")
    moderate = propose("traffic_detector.moderate_min", tcfg.moderate_min, mod_pos, mod_neg,
                       "fill where light traffic becomes moderate")
    propose("traffic_detector.heavy_min", tcfg.heavy_min, heavy_pos, heavy_neg,
                    "fill where moderate traffic becomes heavy")
    # Keep the levels in order.
    for c in changes:
        if c["key"] == "traffic_detector.moderate_min" and c["new"] <= present:
            c["new"] = round(present * 1.5, 4)
        if c["key"] == "traffic_detector.heavy_min" and c["new"] <= max(moderate, present):
            c["new"] = round(max(moderate, present) * 1.5, 4)
    propose("traffic_detector.deck_clear_max", tcfg.deck_clear_max, deck_pos, deck_neg,
            "deck fill under which the span can read clear")
    closed = propose("bridge_detector.closed_max_deg", bcfg.closed_max_deg, ang_pos, ang_neg,
                     "leaf angle under which a leaf reads closed", digits=1)
    if closed != bcfg.closed_max_deg and bcfg.open_min_deg < closed + 15:
        changes.append({"key": "bridge_detector.open_min_deg", "old": bcfg.open_min_deg,
                        "new": round(closed + 15, 1), "why": "kept 15° above the new closed angle", "evidence": {}})
    return changes


# ---------------------------------------------------------------- replay
def variant(config, changes: list[dict[str, Any]] | None = None):
    """A copy of the config for replay, with the changes applied and the boat model off
    (boats aren't tuned here and the model is the slow part)."""
    from ..config import apply_tuning

    cfg = copy.deepcopy(config)
    cfg.boat_detector.enabled = False
    cfg.untuned = {}
    if changes:
        data: dict[str, dict[str, float]] = {}
        for c in changes:
            section, key = c["key"].split(".", 1)
            data.setdefault(section, {})[key] = c["new"]
        apply_tuning(cfg, data)
    return cfg


def replay(chunks: list[tuple[dict[str, Any], Path, dict]], configs: list, every_s: float = 1.0,
           should_stop: Callable[[], bool] | None = None) -> list[dict[str, list[dict[str, Any]]]]:
    """Run the chunks, in order, through one ``Pipeline`` per config, all fed the same frames.
    Returns, per config, {clip_id: detection-log-shaped entries every ``every_s``}."""
    from ..detectors import Pipeline
    from ..evaluate import _frames

    if not configs:
        return []
    pipes = [Pipeline(cfg, None) for cfg in configs]
    out: list[dict[str, list[dict[str, Any]]]] = [{} for _ in configs]
    base = configs[0]
    size = tuple(base.camera.lores_size)
    last_wall: float | None = None
    for meta, video, rois in chunks:
        if should_stop and should_stop():
            break
        for p in pipes:
            p.rois = rois
        t0 = float(meta["started_at"]) - float(meta.get("prebuffer_s") or 0.0)
        logs = [o.setdefault(meta["id"], []) for o in out]
        next_log = 0.0
        try:
            for t, small, hires in _frames(video, base.detectors.fps, size, base.camera.detector_hires):
                wall = t0 + t
                if last_wall is not None and wall <= last_wall:
                    continue
                last_wall = wall
                for p, log_ in zip(pipes, logs):
                    p.process(wall, small, hires)
                    if t >= next_log:
                        log_.append(_entry(round(t, 2), p.state()))
                if t >= next_log:
                    next_log = t + every_s
        except Exception as exc:
            log.warning("tune: replay of %s stopped early: %s", meta["id"], exc)
    return out


def _entry(t: float, st: dict[str, Any]) -> dict[str, Any]:
    """The same shape as a live detection log entry (autorecord.py)."""
    e: dict[str, Any] = {
        "t": t,
        "bridge": {k: st["bridge"].get(k) for k in ("position", "angle_deg")},
        "boat": {"present": False},
    }
    if st.get("far_leaf"):
        e["far_leaf"] = {k: st["far_leaf"].get(k) for k in ("position", "angle_deg")}
    if st.get("traffic"):
        e["traffic"] = {d: {k: v.get(k) for k in ("level", "flow", "fill")} for d, v in st["traffic"].items()}
    if st.get("deck"):
        e["deck"] = st["deck"]
    if st.get("opening"):
        e["phase"] = st["opening"].get("phase")
    return e


def live_logs(metas: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {m["id"]: readings(m) for m in metas}
