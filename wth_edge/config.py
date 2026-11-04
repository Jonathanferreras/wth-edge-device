"""Configuration: code defaults <- profile <- site <- device file.

* ``profiles/<name>.toml`` describes hardware (camera, display pins, which services run).
* ``sites/<name>.toml`` describes one window's calibration (ROIs, compass directions).
* ``/etc/wth/device.toml`` names the profile and site for this Pi and holds local overrides.

Later layers override earlier ones key by key. Secrets never live in these files; they come
from the environment (systemd loads ``/etc/wth/secrets.env``).
"""

from __future__ import annotations

import copy
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_DEVICE_FILE = Path("/etc/wth/device.toml")
# Repo root: profiles/ and sites/ sit next to the wth_edge package.
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class DeviceSection:
    id: str = "wth-dev"
    profile: str = "dev"
    site: str = "default"


@dataclass
class HardwareSection:
    # Substring expected in /proc/device-tree/model, e.g. "Raspberry Pi 5". Empty = any.
    model_match: str = ""
    needs_spi: bool = False
    needs_camera: bool = False


@dataclass
class CameraSection:
    # "picamera2" on a Pi, "fake" for a test pattern, "file" to replay a video.
    driver: str = "fake"
    main_size: tuple[int, int] = (1280, 720)
    lores_size: tuple[int, int] = (640, 360)
    # Pi 5 can deliver RGB on the lores stream. Pi 4 and older only give YUV420 there,
    # in which case the preview is greyscale (the Y plane).
    lores_format: str = "RGB888"
    framerate: float = 15.0
    # Camera Module 3 autofocus: "manual" with lens_position 0.0 focuses at infinity.
    af_mode: str = "manual"
    lens_position: float = 0.0
    hflip: bool = False
    vflip: bool = False
    # Also hand the detectors the main stream's luma plane (full-res greyscale). The
    # bridge edge is only a few pixels thick and gets lost at lores size.
    detector_hires: bool = True
    file_path: str = ""
    file_loop: bool = True
    # Treat the camera as unhealthy when no frame has arrived for this long.
    stale_after_s: float = 5.0


@dataclass
class DisplaySection:
    # "gc9a01" for the round SPI screen, "png" to write frames to a file, "none".
    driver: str = "none"
    spi_bus: int = 0
    spi_device: int = 0
    spi_hz: int = 40_000_000
    dc_pin: int = 25  # BCM numbering
    rst_pin: int = 27
    backlight_pin: int = -1  # -1 = backlight wired straight to 3.3 V
    # MADCTL register: rotation / mirroring / BGR. 0x08 = BGR, no rotation.
    madctl: int = 0x08
    png_path: str = "display.png"
    refresh_s: float = 1.0
    # The screen shows "offline" when state.json is older than this.
    stale_after_s: float = 15.0
    # Pixel art played once when the screen service starts: a bundled name
    # (`wth-edge art list`), a .gif path, or "" for none.
    boot_animation: str = "boot"


@dataclass
class TelemetrySection:
    interval_s: float = 30.0


@dataclass
class WebSection:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    preview_fps: float = 8.0
    jpeg_quality: int = 70


@dataclass
class PublisherSection:
    # Only "console" exists so far. "firestore" comes with the backend phase.
    driver: str = "console"
    interval_s: float = 60.0


@dataclass
class RecordingSection:
    enabled: bool = True
    # Seconds of video kept in memory, so a clip includes what happened before "start".
    prebuffer_s: float = 15.0
    # A clip stops itself after this long, even if nobody presses stop.
    max_duration_s: float = 600.0
    # H.264 bitrate for the main stream (Pi). 4 Mbit/s at 720p is about 30 MB a minute.
    bitrate: int = 4_000_000
    # Delete the oldest clips not marked "keep" when free disk drops below this.
    min_free_percent: float = 20.0
    labels: tuple[str, ...] = ("boat", "bridge", "traffic", "other")


@dataclass
class DetectorsSection:
    enabled: bool = True
    # How often detectors look at a frame. The camera runs faster; detectors don't need to.
    fps: float = 5.0
    # Write a detection entry into the clip.json of the clip being recorded this often.
    log_interval_s: float = 1.0


@dataclass
class BridgeDetectorSection:
    """Bridge angle from the longest straight edge in the bridge ROI (Canny + Hough).

    Pixel parameters are for a 1280-wide frame, like the old bridge.py, and are scaled
    down for smaller frames. The camera passes full-res greyscale when it can. Angles are 0 (flat deck) to 90 (vertical).
    """

    enabled: bool = True
    closed_max_deg: float = 25.0
    open_min_deg: float = 50.0
    # Lines steeper than this are ignored. 90 keeps all: fully open leaves are near vertical.
    max_line_deg: float = 90.0
    min_line_px: int = 40
    canny_low: int = 40
    canny_high: int = 165
    hough_threshold: int = 40
    # Median of the angles over this window, to ride out a missed frame.
    smooth_s: float = 3.0
    # A new position must hold this long before it is reported.
    hold_s: float = 2.0
    # Degrees per second that count as moving, for opening vs closing.
    min_rate_deg_s: float = 1.0
    # No line for this long -> "unknown".
    lost_s: float = 10.0


@dataclass
class BoatDetectorSection:
    """Motion in the boat ROI wakes a YOLO model (ONNX through OpenCV) that confirms it."""

    enabled: bool = True
    # Smallest moving blob, as a fraction of the ROI area.
    motion_min_area: float = 0.004
    # "yolo": a boat needs a YOLO hit. "motion": a moving blob that travels counts (no model).
    confirm: str = "yolo"
    model_path: str = "models/yolov8s.onnx"
    class_id: int = 8          # COCO "boat"
    confidence: float = 0.25
    input_size: int = 640
    # YOLO runs this often while there is motion or a crossing, and at the idle rate
    # otherwise (slow boats don't register as motion). 0 = only on motion.
    yolo_max_hz: float = 2.0
    yolo_idle_hz: float = 0.5
    # A crossing needs this many sightings before it counts.
    min_hits: int = 2
    # Direction locks once the boat has moved this fraction of the frame width.
    min_travel: float = 0.03
    # The crossing ends after no sighting for this long.
    gone_s: float = 20.0


@dataclass
class AutoRecordSection:
    enabled: bool = True
    on_bridge: bool = True     # record while the bridge is opening / open / closing
    on_boat: bool = True       # record while a boat is crossing
    # Keep recording this long after the bridge is closed and no boat is seen.
    post_roll_s: float = 20.0


@dataclass
class UiSection:
    # How the screen and web panel show temperatures: "F" or "C".
    # Telemetry data itself always stays in Celsius (cpu_temp_c).
    temperature_unit: str = "F"


@dataclass
class PathsSection:
    state_file: str = "/run/wth/state.json"
    data_dir: str = "/var/lib/wth"


@dataclass
class Roi:
    """A region as fractions of the frame (0-1), so it survives resolution changes."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        x1 = max(0, min(width, round(self.x * width)))
        y1 = max(0, min(height, round(self.y * height)))
        x2 = max(0, min(width, round((self.x + self.w) * width)))
        y2 = max(0, min(height, round((self.y + self.h) * height)))
        return x1, y1, x2, y2


@dataclass
class SiteSection:
    name: str = "default"
    left_is: str = "WEST"
    right_is: str = "EAST"
    rois: dict[str, Roi] = field(default_factory=dict)


@dataclass
class Config:
    device: DeviceSection = field(default_factory=DeviceSection)
    hardware: HardwareSection = field(default_factory=HardwareSection)
    camera: CameraSection = field(default_factory=CameraSection)
    display: DisplaySection = field(default_factory=DisplaySection)
    telemetry: TelemetrySection = field(default_factory=TelemetrySection)
    web: WebSection = field(default_factory=WebSection)
    publisher: PublisherSection = field(default_factory=PublisherSection)
    paths: PathsSection = field(default_factory=PathsSection)
    ui: UiSection = field(default_factory=UiSection)
    recording: RecordingSection = field(default_factory=RecordingSection)
    detectors: DetectorsSection = field(default_factory=DetectorsSection)
    bridge_detector: BridgeDetectorSection = field(default_factory=BridgeDetectorSection)
    boat_detector: BoatDetectorSection = field(default_factory=BoatDetectorSection)
    autorecord: AutoRecordSection = field(default_factory=AutoRecordSection)
    site: SiteSection = field(default_factory=SiteSection)
    # Files that were merged, in order, for `wth-edge doctor` and logs.
    sources: list[str] = field(default_factory=list)


class ConfigError(Exception):
    pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            log.warning("ignoring unknown config key %s.%s", where, key)
            continue
        default = getattr(cls(), key)
        if isinstance(default, tuple) and isinstance(value, list):
            value = tuple(value)
        elif isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        kwargs[key] = value
    return cls(**kwargs)


def _build_site(data: dict[str, Any]) -> SiteSection:
    rois_raw = data.get("rois", {})
    rest = {k: v for k, v in data.items() if k != "rois"}
    site = _build(SiteSection, rest, "site")
    site.rois = {name: _build(Roi, roi, f"site.rois.{name}") for name, roi in rois_raw.items()}
    return site


def _search_dirs(kind: str) -> list[Path]:
    dirs = []
    if home := os.environ.get("WTH_HOME"):
        dirs.append(Path(home) / kind)
    dirs.append(REPO_ROOT / kind)
    return dirs


def find_named(kind: str, name: str) -> Path:
    """Locate profiles/<name>.toml or sites/<name>.toml."""
    for d in _search_dirs(kind):
        candidate = d / f"{name}.toml"
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(d) for d in _search_dirs(kind))
    raise ConfigError(f"no {kind[:-1]} named {name!r} (looked in {searched})")


def available(kind: str) -> list[str]:
    names: set[str] = set()
    for d in _search_dirs(kind):
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.toml"))
    return sorted(names)


def load(device_file: Path | str | None = None) -> Config:
    """Load the merged config for this device."""
    if device_file is None:
        device_file = os.environ.get("WTH_CONFIG", DEFAULT_DEVICE_FILE)
    device_path = Path(device_file)
    device_data = _read_toml(device_path)

    dev = device_data.get("device", {})
    profile_name = dev.get("profile", DeviceSection.profile)
    site_name = dev.get("site", DeviceSection.site)

    profile_path = find_named("profiles", profile_name)
    site_path = find_named("sites", site_name)

    merged = deep_merge(_read_toml(profile_path), _read_toml(site_path))
    merged = deep_merge(merged, device_data)
    merged.setdefault("device", {})
    merged["device"].setdefault("profile", profile_name)
    merged["device"].setdefault("site", site_name)

    cfg = from_dict(merged)
    cfg.sources = [str(profile_path), str(site_path), str(device_path)]

    # Relative paths in a dev config resolve against the device file's folder.
    base = device_path.parent
    for section, attr in (("paths", "state_file"), ("paths", "data_dir"), ("display", "png_path"), ("camera", "file_path")):
        obj = getattr(cfg, section)
        value = getattr(obj, attr)
        if value and not Path(value).is_absolute():
            setattr(obj, attr, str((base / value).resolve()))

    # Last layer: ROIs adjusted in the web panel, saved where the service may write.
    overrides = roi_override_path(cfg)
    if overrides.is_file():
        try:
            for name, roi in _read_toml(overrides).get("rois", {}).items():
                cfg.site.rois[name] = _build(Roi, roi, f"rois.{name}")
            cfg.sources.append(str(overrides))
        except ConfigError as exc:
            log.error("ignoring %s: %s", overrides, exc)
    return cfg


ROI_OVERRIDE_FILE = "rois.toml"


def roi_override_path(cfg: Config) -> Path:
    return Path(cfg.paths.data_dir) / ROI_OVERRIDE_FILE


def save_roi_overrides(cfg: Config, rois: dict[str, Roi]) -> Path:
    """Write ROIs edited in the web panel. They win over the site file until deleted."""
    path = roi_override_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by the wth-edge web panel. Delete this file to go back to the site file.", ""]
    for name, roi in sorted(rois.items()):
        lines += [f"[rois.{name}]", f"x = {roi.x:.4f}", f"y = {roi.y:.4f}", f"w = {roi.w:.4f}", f"h = {roi.h:.4f}", ""]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, path)
    return path


def from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    for f in fields(Config):
        if f.name == "sources" or f.name not in data:
            continue
        section = data[f.name]
        if not isinstance(section, dict):
            raise ConfigError(f"[{f.name}] must be a table")
        if f.name == "site":
            cfg.site = _build_site(section)
        else:
            cls = type(getattr(cfg, f.name))
            assert is_dataclass(cls)
            setattr(cfg, f.name, _build(cls, section, f.name))
    for key in data:
        if key not in {f.name for f in fields(Config)}:
            log.warning("ignoring unknown config section [%s]", key)
    return cfg
