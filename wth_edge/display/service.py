"""``wth-edge display``: draw the screen from state.json, forever.

``display.mode`` picks the live pixel art scene (telemetry drawn into it, see
``art/live.py``), the text status screen, or both in turn.

Runs as its own systemd unit (wth-display.service) so the screen keeps working, and
says "offline", when the main app is down.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from ..state import read_state, state_age_s
from .screen import overall, render

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


def art_turn(mode: str, now: float, art_s: float, status_s: float) -> bool:
    """Whether the art scene (rather than the text status) is on screen at ``now``."""
    if mode == "art":
        return True
    if mode == "status":
        return False
    return (now % (art_s + status_s)) < art_s


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

    if dcfg.boot_animation:
        try:
            from .art.player import load, play

            play(dev, load(dcfg.boot_animation), loops=1, seconds=10, stop=stop)
        except Exception as exc:  # a broken GIF must never keep the status screen from starting
            log.warning("boot animation %r failed: %s", dcfg.boot_animation, exc)

    mode = dcfg.mode.lower()
    if mode not in ("art", "status", "alternate"):
        log.warning("unknown display.mode %r, using 'status'", dcfg.mode)
        mode = "status"
    live = None
    if mode != "status":
        from .art.live import LiveScene

        live = LiveScene(config.ui.temperature_unit, tuple(dcfg.night_hours))
    log.info("display %s in %s mode, state from %s", dcfg.driver, mode, config.paths.state_file)
    last = None
    state, state_read_at = None, 0.0
    try:
        while not stop.is_set():
            now = time.time()
            if now - state_read_at >= 0.5:  # state.json changes about once a second
                state, state_read_at = read_state(config.paths.state_file), now
            age = state_age_s(state, now)
            if live and art_turn(mode, now, dcfg.art_s, dcfg.status_s):
                t0 = time.monotonic()
                dev.show(live.frame(state, overall(state, age, dcfg.stale_after_s), age, now))
                last = None
                stop.wait(max(0.0, 1.0 / dcfg.art_fps - (time.monotonic() - t0)))
                continue
            img = render(state, age, dcfg.stale_after_s, temp_unit=config.ui.temperature_unit)
            raw = img.tobytes()
            if raw != last:  # SPI writes cost ~25 ms; skip identical frames
                dev.show(img)
                last = raw
            stop.wait(dcfg.refresh_s if mode == "status" else min(dcfg.refresh_s, 0.5))
    finally:
        dev.close()
    return 0
