"""Start and stop clips from detector state, for training data.

A clip starts when the opening state machine leaves ``flowing`` (gates down, so the
run-up to every opening is in the clip) or a boat crossing starts, and stops
``post_roll_s`` after both are quiet again. The recorder's pre-roll means the clip already
holds the seconds before the trigger. Without the state machine the bridge position
(opening, open, closing) triggers instead.

Manual clips are never stopped from here, and neither are self-tuning chunks (trigger
``tune``): while a tuning session holds the recorder, auto-recording stands aside. When an automatic clip hits the maximum length,
or someone stops it from the panel, while the bridge is still up or the boat still there, a
new clip does not start until things go quiet. So pressing Stop sticks, and a stuck
detector can't fill the disk with back-to-back clips.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .recording import RecordingError

log = logging.getLogger(__name__)

MOVING = ("opening", "open", "closing")
# With the state machine running, a clip covers the whole opening from the gates going down.
ACTIVE_PHASES = ("gates_down", "lifting", "raised", "lowering", "releasing")


class AutoRecorder:
    def __init__(self, cfg, log_interval_s: float, recorder) -> None:
        self.cfg = cfg
        self.recorder = recorder
        self.log_interval_s = log_interval_s
        self._quiet_since: float | None = None
        self._last_log = 0.0
        self._blocked = False  # after a max-length stop, until quiet

    def reason(self, st: dict[str, Any]) -> str | None:
        if self.cfg.on_boat and (st.get("boat") or {}).get("present"):
            return "boat"
        if self.cfg.on_bridge:
            opening = st.get("opening") or {}
            if opening.get("phase") in ACTIVE_PHASES:
                return "bridge"
            if not opening and (st.get("bridge") or {}).get("position") in MOVING:
                return "bridge"
        return None

    def __call__(self, events: list[dict[str, Any]], st: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        rec = self.recorder
        # Detections go into whatever clip is recording, manual ones included.
        if rec.active_trigger is not None:
            for e in events:
                rec.log_detection({"event": e}, at=now)
            if now - self._last_log >= self.log_interval_s:
                self._last_log = now
                entry = {
                    "bridge": {k: st["bridge"].get(k) for k in ("position", "angle_deg", "confidence", "line")},
                    "boat": {k: st["boat"].get(k) for k in ("present", "direction", "confidence", "box")},
                }
                if st.get("far_leaf"):
                    entry["far_leaf"] = {k: st["far_leaf"].get(k) for k in ("position", "angle_deg", "line")}
                if st.get("traffic"):
                    entry["traffic"] = {d: {k: v.get(k) for k in ("level", "flow", "fill")} for d, v in st["traffic"].items()}
                if (st.get("gates") or {}).get("arms"):
                    entry["gates"] = {n: g.get("position") for n, g in st["gates"]["arms"].items()}
                if st.get("deck"):
                    entry["deck"] = st["deck"]
                if st.get("opening"):
                    entry["phase"] = st["opening"].get("phase")
                if (st.get("ais") or {}).get("approaching"):
                    a = st["ais"]["approaching"]
                    entry["ais"] = {k: a.get(k) for k in ("mmsi", "name", "side", "distance_m", "eta_s")}
                rec.log_detection(entry, at=now)
        if not self.cfg.enabled or not rec.available:
            return
        if getattr(rec, "reserved_by", None) is not None or rec.active_trigger == "tune":
            # A self-tuning session is recording everything already; stay out of its way.
            self._quiet_since, self._blocked = None, False
            return

        why = self.reason(st)
        if why is None:
            if self._quiet_since is None:
                self._quiet_since = now
            self._blocked = False
            rec.hold_auto = None  # only matters while the same event continues
        else:
            self._quiet_since = None

        trigger = rec.active_trigger
        if trigger is None:
            if why and not self._blocked:
                if rec.hold_auto is not None:
                    # The last auto clip ran out, or was stopped by hand, while this continued.
                    rec.hold_auto = None
                    self._blocked = True
                    log.info("auto-record: clip ended while %s continues; waiting for quiet", why)
                    return
                try:
                    rec.start(trigger=why, label=why, note="auto")
                except RecordingError as exc:
                    log.warning("auto-record could not start: %s", exc)
        elif trigger != "manual" and why is None and now - (self._quiet_since or now) >= self.cfg.post_roll_s:
            log.info("auto-record: quiet for %.0f s, stopping", self.cfg.post_roll_s)
            rec.stop()
