# Contributing to wth-edge-device

- Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11). Package: `wth_edge/`. Entry point: `wth-edge`.
- Hardware differences go in `profiles/*.toml`, window calibration in `sites/*.toml`. Never hardcode pins,
  resolutions or pixel ROIs in code. ROIs are fractions of the frame.
- One process owns the camera. Consumers read frames from the `FrameBus`; nothing else opens the camera.
- Detectors and telemetry hand data to a `Publisher`; they never talk to a backend directly.
- Bridge state follows the web app's rules: when the bridge isn't `closed`, traffic is `standstill` or
  `unknown`; wait time only exists while `opening`, `open` or `closing`.
- Pi-only imports (`picamera2`, `libcamera`, `spidev`, `gpiozero`) stay inside the driver that needs them, so
  everything else runs and tests on a laptop.
- `install.sh` must stay idempotent and non-interactive.
- Check with `pytest` before pushing. `legacy/` is the Pi 3 code, kept for reference; don't extend it.
