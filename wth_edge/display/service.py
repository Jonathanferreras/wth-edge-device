"""``wth-edge display``: redraw the status screen from state.json, forever.

Runs as its own systemd unit (wth-display.service) so the screen keeps working, and
says "offline", when the main app is down.
"""

from __future__ import annotations

import logging
import signal
import threading

from ..state import read_state, state_age_s
from .screen import render

log = logging.getLogger(__name__)


def make_display(cfg):
    driver = cfg.driver.lower()
    if driver == "gc9a01":
        from .gc9a01 import GC9A01

        return GC9A01(cfg)
    if driver == "png":
        from .gc9a01 import PngDisplay

        return PngDisplay(cfg)
    if driver == "none":
        return None
    raise ValueError(f"unknown display.driver {cfg.driver!r} (gc9a01, png, none)")


def run_display(config, stop: threading.Event | None = None) -> int:
    dcfg = config.display
    dev = make_display(dcfg)
    if dev is None:
        log.info("display.driver is 'none'; nothing to do")
        return 0
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())

    log.info("display %s refreshing every %.1fs from %s", dcfg.driver, dcfg.refresh_s, config.paths.state_file)
    last = None
    try:
        while not stop.is_set():
            state = read_state(config.paths.state_file)
            img = render(state, state_age_s(state), dcfg.stale_after_s)
            raw = img.tobytes()
            if raw != last:  # SPI writes cost ~25 ms; skip identical frames
                dev.show(img)
                last = raw
            stop.wait(dcfg.refresh_s)
    finally:
        dev.close()
    return 0
