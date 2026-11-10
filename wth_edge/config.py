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
    # What the screen shows: "art" (the live pixel art scene, telemetry drawn into it),
    # "status" (the text status screen) or "alternate" (art for art_s, then status for status_s).
    mode: str = "alternate"
    art_fps: float = 8.0
    art_s: float = 20.0
    status_s: float = 6.0
    # Local hours when the art switches to the sleeping scene: (start, end).
    night_hours: tuple[int, int] = (22, 6)


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
    # "console" logs one JSON line per publish; "firestore" writes to the web app's Firestore.
    driver: str = "console"
    # How often telemetry goes out (devices/{id} in Firestore).
    interval_s: float = 60.0
    # bridgeState/current: a new position goes out at once; a traffic level only after it has
    # held this long, so a lane flickering between two levels doesn't spend writes and reads.
    traffic_hold_s: float = 60.0
    # No two bridge state writes closer than this, except for a position change.
    min_interval_s: float = 30.0
    # Rewrite the unchanged state this often so the website can tell the device is alive.
    heartbeat_s: float = 600.0
    # Fields to send as unknown until their detector is trusted: "traffic", "wait", "position".
    withhold: tuple[str, ...] = ()
    # Firestore: service account key (JSON) and project. project_id defaults to the key's.
    credentials_file: str = "/etc/wth/firebase-service-account.json"
    project_id: str = ""
    # While an admin has taken over (acceptsDeviceUpdates false), check again this often.
    override_check_s: float = 60.0


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
class GateDetectorSection:
    """The barrier arms at the ends of the span, one box per arm (``gate_*`` ROIs).

    An arm standing up (about 90 degrees) lets traffic through; the arms come down before
    the bridge opens. Each box is drawn around a raised arm, so a long near-vertical line in
    it means "up". A lowered arm lies across the road, out of its narrow box, so "down" is
    the arm going missing from a box where it is normally seen. A horizontal line is no
    help: the road and the field edge give one with the arm up. Pixel parameters are for a
    1280-wide frame and scale with it.
    """

    enabled: bool = True
    canny_low: int = 40
    canny_high: int = 150
    hough_threshold: int = 15
    max_line_gap_px: int = 4
    # "up": a line at least this steep, at least this share of the box height.
    up_min_deg: float = 70.0
    up_min_frac: float = 0.45
    # A box darker than this (mean grey 0-255) can't show a missing arm: "unknown", not "down".
    dark_below: float = 40.0
    # The arm counts as up while it was seen this recently...
    up_window_s: float = 5.0
    # ...and down once it has been missing this long, from a box where it is usually seen:
    # at least trust_min of the seconds over the last trust_s.
    down_after_s: float = 8.0
    trust_s: float = 120.0
    trust_min: float = 0.5


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
class TrafficDetectorSection:
    """Traffic flow and fill per lane box (``northbound``, ``southbound``) and the ``deck``.

    Two cheap measures on the lores frame: *flow* is how much of the box changed since the
    previous detector frame (cars moving), *fill* is how much of the box differs from a slow
    background (cars present, moving or not). Levels follow the web app: light, moderate,
    heavy, standstill, unknown.
    """

    enabled: bool = True
    # Boxes are shrunk to this many pixels wide before measuring.
    work_width: int = 160
    # Pixel difference (0-255) that counts as a change.
    diff_threshold: int = 24
    # Fill below this is an empty lane. Boxes are mostly road, so a few percent is a car;
    # the panel shows live flow and fill per lane to tune these.
    present_min: float = 0.02
    # Fill thresholds for light / moderate / heavy while traffic moves.
    moderate_min: float = 0.06
    heavy_min: float = 0.15
    # Flow below this is "not moving". Standstill = present and not moving for standstill_s.
    flow_min: float = 0.003
    standstill_s: float = 8.0
    # Measures are averaged over this window.
    window_s: float = 6.0
    # A level must hold this long before it is reported.
    hold_s: float = 3.0
    # Background learning rate per detector frame (small = cars stay "foreground" for minutes).
    background_rate: float = 0.002
    # The deck is clear when both lanes are at a standstill, or when its fill is below
    # deck_clear_max and no car has crossed it (no frame with flow or a moving car blob)
    # for deck_clear_s.
    deck_clear_max: float = 0.03
    deck_clear_s: float = 5.0
    # Boxes around the cars in each lane, for the panel. A blob smaller than this fraction
    # of its lane box is ignored; at most vehicle_max per box.
    vehicle_boxes: bool = True
    vehicle_min_area: float = 0.01
    vehicle_max: int = 12


@dataclass
class StagesSection:
    """Staged attention: which detectors run, so the device doesn't check everything at once.

    traffic (lanes and deck only) -> watch (plus leaves and boats) -> track (the bridge is
    moving: leaves and boat at full rate). See ``wth_edge/detectors/stages.py``.
    ``enabled = false`` runs every detector on every frame, as before.
    """

    enabled: bool = True
    # traffic -> watch: no lane moving (both stopped, or unreadable) for this long.
    stopped_s: float = 15.0
    # watch -> traffic: no leaf movement for this long...
    quiet_s: float = 30.0
    # ...or this long after the last boat sighting (camera or AIS), whichever is later.
    boat_quiet_s: float = 120.0
    # watch -> traffic: cars moving in either direction for this long.
    resume_s: float = 5.0
    # track -> watch: all leaves flat for this long. Long enough to ride out a leaf
    # reading that flickers to "closed" mid-opening.
    flat_s: float = 15.0
    # track -> watch: no leaf readable for this long.
    lost_s: float = 30.0


@dataclass
class OpeningSection:
    """The bridge opening state machine (see docs: Bridge opening state machine).

    Phases: flowing, gates_down, lifting, raised, lowering, releasing, unknown. Durations are
    seconds and are the defaults for the wait estimate until the device has seen openings of
    its own (kept in ``<data_dir>/openings.json``).
    """

    enabled: bool = True
    # Both directions standstill with the deck clear this long -> gates_down.
    gates_down_hold_s: float = 20.0
    # The same with the barrier arms seen down: the strongest sign an opening is coming.
    gates_seen_hold_s: float = 3.0
    # gates_down with no lift and no boat for this long -> back to flowing.
    false_alarm_s: float = 180.0
    # Leaf steady and high this long -> raised; flat this long -> releasing.
    raised_hold_s: float = 3.0
    flat_hold_s: float = 3.0
    # Either direction moving again this long -> flowing.
    release_hold_s: float = 10.0
    # No usable traffic or leaf signal for this long -> unknown.
    unknown_after_s: float = 30.0
    # Typical phase durations until the device has its own history.
    default_gates_down_s: float = 60.0
    default_lifting_s: float = 120.0
    default_raised_s: float = 60.0
    default_lowering_s: float = 120.0
    default_releasing_s: float = 30.0
    # How many past openings the wait estimate remembers.
    history: int = 30


@dataclass
class WeatherSection:
    """Local weather from Open-Meteo (free, no key) for the screen and for visibility checks.

    The location is the site's ``bridge_lat``/``bridge_lon``; set ``lat``/``lon`` here to override.
    """

    enabled: bool = False
    interval_s: float = 900.0
    timeout_s: float = 10.0
    lat: float = 0.0
    lon: float = 0.0
    # Visibility below this (metres) is flagged in the state as too low for the bridge detector.
    lowvis_below_m: float = 1000.0


@dataclass
class AisSection:
    """Vessel positions from AIS, so boats are known before the camera sees them.

    ``driver``: "none", or "aisstream" (aisstream.io websocket; key in ``WTH_AISSTREAM_KEY``).
    A local receiver (RTL-SDR + AIS-catcher) can feed the same interface later.
    """

    enabled: bool = False
    driver: str = "aisstream"
    url: str = "wss://stream.aisstream.io/v0/stream"
    # Vessels within this distance of the bridge are tracked.
    radius_m: float = 1600.0
    # A vessel is "approaching" when closing on the bridge at least this fast (knots).
    approach_min_knots: float = 0.5
    # Forget a vessel after this long without a report.
    stale_s: float = 300.0
    # Reconnect wait after a dropped connection.
    retry_s: float = 10.0


@dataclass
class AutoRecordSection:
    enabled: bool = True
    on_bridge: bool = True     # record from gates_down until traffic flows again
    on_boat: bool = True       # record while a boat is crossing
    # Keep recording this long after the bridge is closed and no boat is seen.
    post_roll_s: float = 20.0


@dataclass
class TuneSection:
    """Self-tuning: record a while, check what the detector said against what was really
    there, fit the thresholds and report (see ``wth_edge/tune/``). Started from the panel."""

    # Whether the panel offers it at all.
    enabled: bool = True
    # How long one round records, and the length of each recorded chunk.
    duration_s: float = 3600.0
    chunk_s: float = 300.0
    # A reviewed moment every this many seconds, plus every time the detector's reading changes.
    sample_s: float = 15.0
    # Each moment is a pair of frames this far apart, so stopped and moving cars differ.
    pair_gap_s: float = 2.0
    # At most this many reviewed moments per round (keeps the API bill bounded).
    max_reviews: int = 360
    # Who labels the frames: "claude-code" (Claude Code on the Pi, run as you by
    # wth-tune-review.timer), "api" (the Anthropic API from the service, ANTHROPIC_API_KEY),
    # or "none" (record, count and report only).
    reviewer: str = "claude-code"
    # Claude Code: the command (default: claude on PATH or ~/.local/bin/claude), the model
    # ("" = Claude Code's default), moments per run, and how long one run may take.
    claude_command: str = ""
    claude_code_model: str = ""
    claude_code_batch: int = 12
    claude_code_timeout_s: float = 900.0
    # After the recording ends, wait at most this long for the reviews to come back.
    review_wait_s: float = 10800.0
    # The API reviewer's model and effort.
    model: str = "claude-opus-5-5"
    effort: str = "low"
    # The offline reference pass: YOLO over the lanes and the river, this many times a second.
    ref_fps: float = 2.0
    ref_boat_fps: float = 0.5
    ref_confidence: float = 0.3
    # Keep tuning (another round) until the reviewed accuracy reaches this, for at most max_rounds.
    target_accuracy: float = 0.9
    max_rounds: int = 3
    # New settings are kept only when the replay beats the old ones by at least this much.
    min_gain: float = 0.02
    auto_apply: bool = True
    # Keep the recorded video after the analysis (normally deleted to save space).
    keep_footage: bool = False
    # Frames where the detector and the review disagreed, kept with the report.
    keep_frames: int = 24


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
    # Which approaches the camera can see a boat on, so a missing boat on a blind side
    # never counts against an opening.
    approach_sides: tuple[str, ...] = ("east",)
    # The bridge's position, for AIS distances. 0,0 = not set.
    bridge_lat: float = 0.0
    bridge_lon: float = 0.0
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
    gate_detector: GateDetectorSection = field(default_factory=GateDetectorSection)
    boat_detector: BoatDetectorSection = field(default_factory=BoatDetectorSection)
    traffic_detector: TrafficDetectorSection = field(default_factory=TrafficDetectorSection)
    opening: OpeningSection = field(default_factory=OpeningSection)
    stages: StagesSection = field(default_factory=StagesSection)
    ais: AisSection = field(default_factory=AisSection)
    weather: WeatherSection = field(default_factory=WeatherSection)
    autorecord: AutoRecordSection = field(default_factory=AutoRecordSection)
    tune: TuneSection = field(default_factory=TuneSection)
    site: SiteSection = field(default_factory=SiteSection)
    # Files that were merged, in order, for `wth-edge doctor` and logs.
    sources: list[str] = field(default_factory=list)
    # Values the self-tuning loop replaced, as they were before ("section.key": value), so
    # the panel can undo the tuning without a restart.
    untuned: dict[str, Any] = field(default_factory=dict)


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

    # Thresholds the self-tuning loop settled on (wth_edge/tune). Delete the file to undo.
    tuned = tuning_override_path(cfg)
    if tuned.is_file():
        try:
            for dotted, (old, _new) in apply_tuning(cfg, _read_toml(tuned)).items():
                cfg.untuned.setdefault(dotted, old)
            cfg.sources.append(str(tuned))
        except ConfigError as exc:
            log.error("ignoring %s: %s", tuned, exc)

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


TUNING_FILE = "tuning.toml"
# What the self-tuning loop may change, as (section, key).
TUNABLE = (
    ("traffic_detector", "flow_min"),
    ("traffic_detector", "present_min"),
    ("traffic_detector", "moderate_min"),
    ("traffic_detector", "heavy_min"),
    ("traffic_detector", "deck_clear_max"),
    ("bridge_detector", "closed_max_deg"),
    ("bridge_detector", "open_min_deg"),
)


def tuning_override_path(cfg: Config) -> Path:
    return Path(cfg.paths.data_dir) / TUNING_FILE


def apply_tuning(cfg: Config, data: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Set tuned values on the (live) config in place. Returns {"section.key": (old, new)}.
    Only keys in TUNABLE are taken; the detectors read these attributes every frame."""
    changed = {}
    allowed = set(TUNABLE)
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if (section, key) not in allowed or isinstance(value, bool) or not isinstance(value, (int, float)):
                log.warning("ignoring tuned value %s.%s", section, key)
                continue
            obj = getattr(cfg, section)
            old = getattr(obj, key)
            setattr(obj, key, float(value))
            changed[f"{section}.{key}"] = (old, float(value))
    return changed


def save_tuning(cfg: Config, values: dict[str, float], note: str = "") -> Path:
    """Write the tuned values (merged with what the file already holds)."""
    path = tuning_override_path(cfg)
    current: dict[str, Any] = _read_toml(path) if path.is_file() else {}
    for dotted, value in values.items():
        section, key = dotted.split(".", 1)
        current.setdefault(section, {})[key] = float(value)
    lines = ["# Written by the self-tuning loop (wth-edge tune). Delete this file to go back.",
             f"# {note}" if note else "", ""]
    for section in sorted(current):
        lines.append(f"[{section}]")
        for key in sorted(current[section]):
            lines.append(f"{key} = {float(current[section][key]):.6g}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, path)
    return path


def from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    for f in fields(Config):
        if f.name in ("sources", "untuned") or f.name not in data:
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
