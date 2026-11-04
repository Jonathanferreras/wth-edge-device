"""``wth-edge doctor``: check that this Pi is set up the way its profile says.

Prints one line per check and exits non-zero if any required check fails, so
install.sh and humans can both use it.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import sys
import urllib.request
from pathlib import Path

from . import telemetry
from .state import read_state, state_age_s

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _line(level: str, name: str, detail: str) -> tuple[str, str, str]:
    mark = {"ok": "✓", "warn": "!", "FAIL": "✗"}[level]
    print(f"  {mark} {name:<22} {detail}")
    return level, name, detail


def run_doctor(config) -> int:
    results = []
    hw = config.hardware
    print(f"wth-edge doctor · device {config.device.id} · profile {config.device.profile} · site {config.device.site}")

    v = sys.version_info
    results.append(_line(OK if v >= (3, 11) else FAIL, "python", f"{v.major}.{v.minor}.{v.micro}"))

    model = telemetry.board_model()
    if hw.model_match:
        good = bool(model and hw.model_match in model)
        results.append(_line(OK if good else WARN, "board", f"{model or 'not a Pi'} (profile expects '{hw.model_match}')"))
    else:
        results.append(_line(OK, "board", model or "not a Pi (no model required)"))

    if config.camera.driver == "picamera2":
        try:
            import picamera2  # noqa: F401  (apt: python3-picamera2)
            from .camera.picam import camera_info

            cams = camera_info()
            if cams:
                results.append(_line(OK, "camera", ", ".join(c.get("Model", "?") for c in cams)))
            else:
                results.append(_line(FAIL, "camera", "none detected; check the ribbon cable, then `rpicam-hello --list-cameras`"))
        except Exception as exc:  # picamera2 missing or libcamera error
            results.append(_line(FAIL, "camera", f"picamera2 unavailable: {exc}"))
    else:
        results.append(_line(OK, "camera", f"driver '{config.camera.driver}' (no hardware check)"))

    if config.display.driver == "gc9a01":
        dev = Path(f"/dev/spidev{config.display.spi_bus}.{config.display.spi_device}")
        results.append(_line(OK if dev.exists() else FAIL, "spi", f"{dev} {'present' if dev.exists() else 'missing; run `sudo raspi-config nonint do_spi 0` and reboot'}"))
        for mod in ("spidev", "gpiozero"):
            try:
                __import__(mod)
                results.append(_line(OK, f"python: {mod}", "importable"))
            except ImportError:
                results.append(_line(FAIL, f"python: {mod}", f"missing; apt install python3-{mod}"))

    if os.geteuid() == 0:
        try:
            groups = {g.gr_name for g in grp.getgrall() if "wth" in g.gr_mem}
            pwd.getpwnam("wth")
            need = {"video"} | ({"spi", "gpio"} if config.display.driver == "gc9a01" else set())
            missing = sorted(n for n in need - groups if _group_exists(n))
            results.append(_line(FAIL if missing else OK, "service user", "wth" + (f" missing groups: {', '.join(missing)}" if missing else "")))
        except KeyError:
            results.append(_line(WARN, "service user", "no 'wth' user (install.sh creates it)"))

    results.append(_line(OK if shutil.which("vcgencmd") else WARN, "vcgencmd", "present" if shutil.which("vcgencmd") else "missing (no voltage / throttle data)"))
    thr = telemetry.throttled()
    if thr:
        bad = thr["under_voltage_now"] or thr["throttled_now"]
        past = thr["under_voltage_occurred"] or thr["throttled_occurred"]
        results.append(_line(FAIL if bad else WARN if past else OK, "power", f"throttled={thr['raw']}" + (" (undervoltage or throttling seen; check the PSU)" if bad or past else "")))
    volts = telemetry.input_voltage_v()
    if volts is not None:
        results.append(_line(OK if volts >= 4.85 else WARN, "input voltage", f"{volts:.2f} V"))

    data_dir = Path(config.paths.data_dir)
    if data_dir.exists():
        free = shutil.disk_usage(data_dir).free / 1e9
        results.append(_line(OK if free > 2 else WARN, "disk", f"{free:.1f} GB free in {data_dir}"))
    else:
        results.append(_line(WARN, "disk", f"{data_dir} does not exist yet"))

    st = read_state(config.paths.state_file)
    age = state_age_s(st)
    if st is None:
        results.append(_line(WARN, "wth-edge service", f"no state at {config.paths.state_file} (not running?)"))
    else:
        level = OK if age is not None and age < 10 else WARN
        results.append(_line(level, "wth-edge service", f"status {st.get('status')}, state {age:.0f}s old"))

    if config.web.enabled:
        url = f"http://127.0.0.1:{config.web.port}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                body = json.loads(r.read())
            results.append(_line(OK, "web panel", f"port {config.web.port} answering ({body.get('status')})"))
        except Exception as exc:
            results.append(_line(WARN, "web panel", f"{url} not answering ({type(exc).__name__})"))
        if not os.environ.get("WTH_WEB_PASSWORD") and os.geteuid() == 0:
            secrets_file = Path("/etc/wth/secrets.env")
            has = secrets_file.exists() and "WTH_WEB_PASSWORD=" in secrets_file.read_text()
            results.append(_line(OK if has else WARN, "web password", "set in /etc/wth/secrets.env" if has else "not set: panel is open on the LAN"))

    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print(f"\n{len(fails)} failed, {len(warns)} warnings")
    return 1 if fails else 0


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False
