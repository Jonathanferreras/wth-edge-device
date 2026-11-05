# Contributing to wth-edge-device

- Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11). Package: `wth_edge/`. Entry point: `wth-edge`.
- Hardware differences go in `profiles/*.toml`, window calibration in `sites/*.toml`. Never hardcode pins,
  resolutions or pixel ROIs in code. ROIs are fractions of the frame.
- ROIs edited in the web panel are saved to `<data_dir>/rois.toml`, which overrides the site file.
- Recording goes through `Recorder` and the camera's `clip_writer` (`begin(path)` / `end()`); every clip gets
  a `clip.json` sidecar in the `ClipStore` layout. Imported and detector-triggered clips use the same format.
- Detectors live in `wth_edge/detectors/`. `Pipeline` is frame in, events and state out, with no threads, so
  `wth-edge eval` and the device run the same code. Keep it that way; threading goes in `DetectorRunner`.
  Detector boxes, lines and ROIs are frame fractions. Thin or small things need `Frame.hires`, not the lores frame.
- One process owns the camera. Consumers read frames from the `FrameBus`; nothing else opens the camera.
- Detectors and telemetry hand data to a `Publisher`; they never talk to a backend directly.
- `wth_edge/opening.py` is the opening state machine (flowing, gates_down, lifting, raised, lowering,
  releasing, unknown). It is pure like `Pipeline`; signals go in through `Signals`, and `website()` is the
  only place that maps a phase to the web app's fields. A boat never starts an opening on its own.
- Bridge state follows the web app's rules: when the bridge isn't `closed`, traffic is `standstill` or
  `unknown`; wait time only exists while `opening`, `open` or `closing`. Keep that in `website()`.
- External sources (AIS today) are threads that fill a table; the detector loop reads a snapshot and hands
  it to `Pipeline.process`, so `eval` can replay without them.
- Pi-only imports (`picamera2`, `libcamera`, `spidev`, `gpiozero`) stay inside the driver that needs them, so
  everything else runs and tests on a laptop.
- `install.sh` must stay idempotent and non-interactive. `update.sh` (run by `wth-update.timer` and
  `sudo wth-edge update`) checks out the `[update] branch` and re-runs it, so anything a new version needs
  on the Pi (packages, units, folders) goes in `install.sh`. Never restart `wth-update.service` from it.
- Check with `pytest` before pushing. `legacy/` is the Pi 3 code, kept for reference; don't extend it.
