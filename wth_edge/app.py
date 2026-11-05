"""``wth-edge run``: the main device process.

Owns the camera, keeps the shared state file fresh for the display service, collects
telemetry, runs the detectors on the same FrameBus, records clips (by hand or when the
detectors see something), hands data to the publisher, and serves the local web panel.
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
from .autorecord import AutoRecorder
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
        self.detectors = None
        self.ais = None
        if config.ais.enabled:
            from .ais import AisSource

            self.ais = AisSource(config.ais, config.site.bridge_lat, config.site.bridge_lon)
        self.weather = None
        if config.weather.enabled:
            from .weather import WeatherSource

            lat = config.weather.lat or config.site.bridge_lat
            lon = config.weather.lon or config.site.bridge_lon
            if lat or lon:
                self.weather = WeatherSource(config.weather, lat, lon)
            else:
                log.warning("weather off: set site.bridge_lat/bridge_lon (or weather.lat/lon)")
        if config.detectors.enabled and config.camera.driver == "fake":
            log.info("detectors off: the test-pattern camera would only trigger false events")
        elif config.detectors.enabled:
            from .detectors import DetectorRunner, opencv_available

            if opencv_available():
                from .opening import HISTORY_FILE, OpeningHistory

                history = OpeningHistory(Path(config.paths.data_dir) / HISTORY_FILE, config.opening.history,
                                         self._opening_defaults(config.opening))
                self.detectors = DetectorRunner(config, self.bus, history=history, ais=self.ais)
                self.detectors.add_listener(AutoRecorder(config.autorecord, config.detectors.log_interval_s, self.recorder))
                self.detectors.add_listener(self._on_detections)
            else:
                log.warning("detectors disabled: OpenCV not installed (apt install python3-opencv)")
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state = new_state(config.device.id, __version__)
        self._threads: list[threading.Thread] = []
        self.server = None  # uvicorn server, when the web panel runs
        self.exit_code = 0
        self._last_website: dict[str, Any] | None = None

    @staticmethod
    def _opening_defaults(cfg) -> dict[str, float]:
        return {"gates_down": cfg.default_gates_down_s, "lifting": cfg.default_lifting_s, "raised": cfg.default_raised_s,
                "lowering": cfg.default_lowering_s, "releasing": cfg.default_releasing_s}

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

    def _on_detections(self, events: list[dict[str, Any]], st: dict[str, Any]) -> None:
        with self._lock:
            for key in ("bridge", "far_leaf", "traffic", "deck", "opening", "boat", "detector", "ais"):
                if key in st:
                    self._state[key] = st[key]
        for e in events:
            try:
                self.publisher.publish_event(self.config.device.id, e)
            except Exception:
                self.publisher.ok = False
                log.exception("publisher %s failed", self.publisher.name)
        website = (st.get("opening") or {}).get("website")
        if website is not None:
            # Publish the website fields when they change, with the wait time rounded so a
            # countdown doesn't publish every second.
            key = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in website.items()}
            if key.get("estimatedWaitTime"):
                key["estimatedWaitTime"] = {k: v for k, v in key["estimatedWaitTime"].items()
                                            if k not in ("estimatedTotalMinutes", "remainingMinutes")}
            if key != self._last_website:
                self._last_website = key
                try:
                    self.publisher.publish_bridge_state(self.config.device.id, website)
                except Exception:
                    self.publisher.ok = False
                    log.exception("publisher %s failed", self.publisher.name)

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
            ais = self.ais.snapshot() if self.ais is not None and self.detectors is None else None
            weather = self.weather.snapshot() if self.weather is not None else None
            with self._lock:
                self._state["camera"] = health.as_dict()
                if ais is not None:
                    self._state["ais"] = ais
                if weather is not None:
                    self._state["weather"] = weather
                self._state["publisher"] = self.publisher.status()
                rec = self.recorder.status()
                self._state["recording"] = {k: rec[k] for k in ("available", "recording", "label", "elapsed_s", "trigger")}
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
        if self.ais is not None:
            self.ais.start()
        if self.weather is not None:
            self.weather.start()
        if self.detectors is not None:
            self.detectors.start()
        self._spawn(self._state_loop, "state")
        self._spawn(self._telemetry_loop, "telemetry")
        self._spawn(self._housekeeping_loop, "housekeeping")

    def stop(self) -> None:
        self.stop_event.set()
        if self.detectors is not None:
            self.detectors.stop()  # first, so nothing starts a new clip during shutdown
        try:
            self.recorder.close()
        except Exception:
            log.exception("could not finish the clip being recorded")
        self.camera.stop()
        if self.ais is not None:
            self.ais.stop()
        if self.weather is not None:
            self.weather.stop()
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
            # uvicorn handles SIGINT / SIGTERM, then re-raises the signal once it has shut
            # down. With the default handler that kills the process before device.stop()
            # runs (and the clip being recorded is never finished), so give it a harmless one.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: device.stop_event.set())
            server.run()
        else:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: device.stop_event.set())
            device.stop_event.wait()
    finally:
        device.stop()
    return device.exit_code
