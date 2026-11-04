# wth-edge-device

The Watch The Hutch edge device: a Raspberry Pi at a window that watches the bridge and
river, shows its status on a small round screen, and (soon) publishes bridge state to the
[web app](https://github.com/Jonathanferreras/watch-the-hutch-web).

This is **v2**, built to be redeployed onto any Pi from a fresh SD card. Hardware is
described by a profile, calibration by a site file, and everything runs as systemd
services. The Pi 3 version lives in [`legacy/`](legacy/) until v2 replaces it.

## Bring up a new Pi

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In its settings set
   the hostname, user, Wi-Fi and your SSH key.
2. Clone and install:

   ```sh
   sudo git clone https://github.com/Jonathanferreras/wth-edge-device /opt/wth-edge
   sudo /opt/wth-edge/install.sh --profile pi5-cam3-gc9a01 --device-id hutch-01
   ```

3. If the installer says SPI was just enabled, `sudo reboot`.
4. Open the web panel at `http://<hostname>.local:8080`. The installer prints the login
   (`wth` / a generated password kept in `/etc/wth/secrets.env`).

`install.sh` is safe to run again. To update later: `sudo wth-edge update` (git pull +
reinstall). To remove: `sudo /opt/wth-edge/uninstall.sh` (add `--purge` to delete config
and data too).

## What runs

| Unit | Command | Does |
| --- | --- | --- |
| `wth-edge.service` | `wth-edge run` | Owns the camera (Picamera2), collects telemetry, writes `/run/wth/state.json`, serves the web panel on port 8080, hands data to the publisher. |
| `wth-display.service` | `wth-edge display` | Redraws the round GC9A01 screen from `state.json` every second. Separate on purpose: it shows **OFFLINE** when the main app is down. |

Useful commands on the Pi:

```sh
wth-edge doctor                 # checks camera, SPI, power, services, web panel
journalctl -u wth-edge -f       # logs
systemctl restart wth-edge
wth-edge config                 # the merged config as JSON
```

## Configuration

Layers, later ones win: code defaults → `profiles/<profile>.toml` → `sites/<site>.toml` →
`/etc/wth/device.toml`.

| File | Holds | In git |
| --- | --- | --- |
| `profiles/*.toml` | Hardware: camera model and resolution, screen driver and pins, which services run | yes |
| `sites/*.toml` | One window's calibration: ROIs as fractions of the frame, compass directions | yes |
| `/etc/wth/device.toml` | This Pi's id, profile, site, and any local overrides | no |
| `/etc/wth/secrets.env` | `WTH_WEB_PASSWORD` and, later, backend credentials | no |

Profiles today:

- `pi5-cam3-gc9a01`: Pi 5, Camera Module 3 NoIR, GC9A01 screen (DC GPIO 25, RST GPIO 27, CE0).
- `pi3-cam2-headless`: the original Pi 3 with no screen (greyscale preview).
- `dev`: a laptop, with a synthetic camera and the screen written to a PNG.

## Develop on a laptop

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
wth-edge --config dev/device.toml run       # web panel on http://127.0.0.1:8080
wth-edge --config dev/device.toml display   # screen frames go to dev/display.png
pytest
```

To replay a recorded clip instead of the test pattern, `pip install -e '.[replay]'` and set
in `dev/device.toml`:

```toml
[camera]
driver = "file"
file_path = "/path/to/clip.mp4"
```

## Status

Done in this step: install script, profiles, camera service with health checks, status
screen, telemetry (Celsius, Pi 5 input voltage, throttle flags), a console publisher, and a
first web panel (live view with ROIs, telemetry).

Next, per the plan: recording (manual and pre-roll), clip browser, detectors (bridge angle,
boat YOLO), the Firestore publisher, and H.264 streaming through MediaMTX.
