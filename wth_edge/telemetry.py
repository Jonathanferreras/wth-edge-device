"""Device health readings. Every reading degrades to None off a Pi instead of failing.

Fixes from the Pi 3 version: temperature is Celsius (it was labelled F), the input voltage
comes from the Pi 5 PMIC instead of a malformed ``vcgencmd`` call that read core voltage,
and undervoltage / throttling flags are reported.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")
MODEL_FILE = Path("/proc/device-tree/model")

# Bits of `vcgencmd get_throttled`.
THROTTLE_BITS = {
    0: "under_voltage_now",
    1: "freq_capped_now",
    2: "throttled_now",
    3: "soft_temp_limit_now",
    16: "under_voltage_occurred",
    17: "freq_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temp_limit_occurred",
}


def board_model() -> str | None:
    try:
        return MODEL_FILE.read_text().rstrip("\x00\n ")
    except OSError:
        return None


def cpu_temp_c() -> float | None:
    try:
        return round(int(THERMAL_ZONE.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _vcgencmd(*args: str) -> str | None:
    exe = shutil.which("vcgencmd")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout.strip()


def parse_throttled(output: str | None) -> dict[str, Any] | None:
    """``throttled=0x50005`` -> {"raw": "0x50005", "under_voltage_now": True, ...}."""
    if not output:
        return None
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", output)
    if not m:
        return None
    value = int(m.group(1), 16)
    flags: dict[str, Any] = {"raw": m.group(1)}
    for bit, name in THROTTLE_BITS.items():
        flags[name] = bool(value & (1 << bit))
    return flags


def parse_pmic_ext5v(output: str | None) -> float | None:
    """``EXT5V_V volt(24)=5.12765000V`` -> 5.128 (Pi 5 only)."""
    if not output:
        return None
    m = re.search(r"EXT5V_V\s+volt\(\d+\)=([0-9.]+)V", output)
    return round(float(m.group(1)), 3) if m else None


def input_voltage_v() -> float | None:
    return parse_pmic_ext5v(_vcgencmd("pmic_read_adc", "EXT5V_V"))


def throttled() -> dict[str, Any] | None:
    return parse_throttled(_vcgencmd("get_throttled"))


def ip_addresses() -> list[str]:
    addrs = []
    for name, entries in psutil.net_if_addrs().items():
        if name == "lo" or name.startswith(("docker", "veth")):
            continue
        for e in entries:
            if e.family == socket.AF_INET and not e.address.startswith("127."):
                addrs.append(e.address)
    return addrs


def snapshot(data_dir: str | None = None) -> dict[str, Any]:
    disk_path = data_dir if data_dir and Path(data_dir).exists() else "/"
    disk = psutil.disk_usage(disk_path)
    vm = psutil.virtual_memory()
    load1, _, _ = psutil.getloadavg()
    return {
        "at": time.time(),
        "hostname": socket.gethostname(),
        "model": board_model(),
        "ip": ip_addresses(),
        "uptime_s": round(time.time() - psutil.boot_time()),
        # Non-blocking: percentage since the previous call (first call returns 0.0).
        "cpu_percent": psutil.cpu_percent(interval=None),
        "load_1m": round(load1, 2),
        "cpu_temp_c": cpu_temp_c(),
        "ram_percent": vm.percent,
        "disk_percent": disk.percent,
        "disk_free_gb": round(disk.free / 1e9, 2),
        "input_voltage_v": input_voltage_v(),
        "throttled": throttled(),
    }
