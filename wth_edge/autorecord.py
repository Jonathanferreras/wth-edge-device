"""Start and stop clips from detector state, for training data.

A clip starts when the bridge leaves ``closed`` (opening, open, closing) or a boat
crossing starts, and stops ``post_roll_s`` after both are quiet again. The recorder's
pre-roll means the clip already holds the seconds before the trigger.

Manual clips are never stopped from here. When an automatic clip hits the maximum length,
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
        if self.cfg.on_bridge and (st.get("bridge") or {}).get("position") in MOVING:
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
                rec.log_detection({
                    "bridge": {k: st["bridge"].get(k) for k in ("position", "angle_deg", "confidence", "line")},
                    "boat": {k: st["boat"].get(k) for k in ("present", "direction", "confidence", "box")},
                }, at=now)
        if not self.cfg.enabled or not rec.available:
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
