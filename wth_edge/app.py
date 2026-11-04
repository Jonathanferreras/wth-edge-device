"""``wth-edge run``: the main device process.

Owns the camera, keeps the shared state file fresh for the display service, collects
telemetry, hands it to the publisher, records clips, and serves the local web panel.
Detectors plug in here in phase 3, reading frames from the same FrameBus.
"""

from __future__ import annotations

import copy
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, telemetry
from .camera import FrameBus, make_camera
from .clips import ClipStore
from .publishers import make_publisher
from .recording import Recorder
from .state import new_state, write_state

log = logging.getLogger(__name__)

STARTUP_GRACE_S = 15.0


class Device:
    def __init__(self, config) -> None:
        self.config = config
        self.bus = FrameBus()
        self.camera = make_camera(config.camera, self.bus)
        self.publisher = make_publisher(config.publisher)
        self.store = ClipStore(config.paths.data_dir)
        self.camera.setup_recording(config.recording)
        self.recorder = Recorder(config, self.camera, self.store)
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state = new_state(config.device.id, __version__)
        self._threads: list[threading.Thread] = []
        self.server = None  # uvicorn server, when the web panel runs
        self.exit_code = 0

    # ---- state shared with the web panel and the display service ----
    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def _update(self, **parts: Any) -> None:
        with self._lock:
            self._state.update(parts)

    def _status(self, cam_ok: bool, tel: dict[str, Any]) -> str:
        if not cam_ok and time.time() - self._state["started_at"] < STARTUP_GRACE_S:
            return "starting"
        thr = tel.get("throttled") or {}
        if not cam_ok or thr.get("under_voltage_now") or thr.get("throttled_now"):
            return "degraded"
        return "ok"

    # ---- loops ----
    def _state_loop(self) -> None:
        path = self.config.paths.state_file
        while not self.stop_event.is_set():
            if self.camera.fatal:
                log.error("camera unrecoverable; exiting so systemd restarts wth-edge")
                self.exit_code = 1
                self.stop_event.set()
                if self.server is not None:
                    self.server.should_exit = True
                break
            health = self.camera.health()
            with self._lock:
                self._state["camera"] = health.as_dict()
                self._state["publisher"] = self.publisher.status()
                rec = self.recorder.status()
                self._state["recording"] = {k: rec[k] for k in ("available", "recording", "label", "elapsed_s")}
                self._state["status"] = self._status(health.ok, self._state["telemetry"])
                self._state["updated_at"] = time.time()
                snapshot = copy.deepcopy(self._state)
            try:
                write_state(path, snapshot)
            except OSError as exc:
                log.error("cannot write %s: %s", path, exc)
            self.stop_event.wait(1.0)

    def _telemetry_loop(self) -> None:
        tcfg, pcfg = self.config.telemetry, self.config.publisher
        telemetry.snapshot(self.config.paths.data_dir)  # prime cpu_percent
        last_publish = 0.0
        while not self.stop_event.is_set():
            try:
                tel = telemetry.snapshot(self.config.paths.data_dir)
                tel["camera"] = self.camera.health().as_dict()
                ips = tel.get("ip") or []
                web_url = f"http://{ips[0]}:{self.config.web.port}" if ips and self.config.web.enabled else None
                self._update(telemetry=tel, web_url=web_url)
                if time.time() - last_publish >= pcfg.interval_s:
                    try:
                        self.publisher.publish_telemetry(self.config.device.id, tel)
                    except Exception:
                        self.publisher.ok = False
                        log.exception("publisher %s failed", self.publisher.name)
                    last_publish = time.time()
            except Exception:
                log.exception("telemetry failed")
            self.stop_event.wait(min(tcfg.interval_s, pcfg.interval_s))

    def _housekeeping_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.store.prune(self.config.recording.min_free_percent, active_id=self.recorder.status()["clip_id"])
            except Exception:
                log.exception("clip cleanup failed")
            self.stop_event.wait(300)

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def start(self) -> None:
        log.info("wth-edge %s starting as %s (config: %s)", __version__, self.config.device.id, ", ".join(self.config.sources))
        try:
            Path(self.config.paths.data_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create data dir %s: %s", self.config.paths.data_dir, exc)
        self.camera.start()
        self._spawn(self._state_loop, "state")
        self._spawn(self._telemetry_loop, "telemetry")
        self._spawn(self._housekeeping_loop, "housekeeping")

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.recorder.close()
        except Exception:
            log.exception("could not finish the clip being recorded")
        self.camera.stop()
        for t in self._threads:
            t.join(timeout=3)
        self.publisher.close()
        log.info("wth-edge stopped")


def run(config) -> int:
    device = Device(config)
    device.start()
    try:
        if config.web.enabled:
            import uvicorn

            from .web.app import create_app

            app = create_app(config, device.bus, device.get_state, recorder=device.recorder, store=device.store)
            server = device.server = uvicorn.Server(
                uvicorn.Config(app, host=config.web.host, port=config.web.port, log_level="warning",
                               timeout_graceful_shutdown=2, access_log=False)
            )
            log.info("web panel on http://%s:%d", config.web.host, config.web.port)
            server.run()  # handles SIGINT / SIGTERM itself
        else:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: device.stop_event.set())
            device.stop_event.wait()
    finally:
        device.stop()
    return device.exit_code
