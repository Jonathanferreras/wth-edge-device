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
   sudo apt update && sudo apt install -y git   # Pi OS Lite has no git
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
| `wth-edge.service` | `wth-edge run` | Owns the camera (Picamera2), records clips, collects telemetry, writes `/run/wth/state.json`, serves the web panel on port 8080, hands data to the publisher. |
| `wth-display.service` | `wth-edge display` | Plays the boot animation, then draws the round GC9A01 screen from `state.json`: the live pixel art scene, the text status, or both in turn (`display.mode`). Separate on purpose: it shows **OFFLINE** when the main app is down. |

Useful commands on the Pi:

```sh
wth-edge doctor                 # checks camera, SPI, power, services, web panel
journalctl -u wth-edge -f       # logs
systemctl restart wth-edge
wth-edge config                 # the merged config as JSON
```

## Configuration

Layers, later ones win: code defaults → `profiles/<profile>.toml` → `sites/<site>.toml` →
`/etc/wth/device.toml` → `/var/lib/wth/rois.toml` (boxes saved from the web panel; delete it
to go back to the site file).

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

## Detectors, the opening state machine and auto-recording

`wth-edge run` watches the boxes in the live view about 5 times a second:

- **Leaves** (`bridge` = the near leaf, `far_leaf` = the far one): the longest straight
  edge in each box (Canny + Hough, ported from the old `bridge.py`) gives the leaf angle,
  0° flat to 90° upright. A 3 s median and a 2 s hold turn it into `closed` / `opening` /
  `open` / `closing` / `unknown` per leaf. The leaves lift one at a time, far leaf first,
  so the far box is the earlier signal. Both read the full-resolution greyscale frame.
- **Traffic** (`northbound`, `southbound`, `deck`): two cheap measures on the preview
  frame per box. *Flow* is how much of the box changed since the last frame (cars moving);
  *fill* is how much differs from a slowly learned background (cars present, moving or
  not). Each lane gets the web app's level: `light` / `moderate` / `heavy` from the fill
  while traffic moves, `standstill` when the lane is filled and nothing moves for 8 s. The
  deck only reports clear or busy. The panel shows live flow and fill per lane so the
  thresholds in `[traffic_detector]` can be tuned on the real view.
- **Boats**: background subtraction in the boat box wakes YOLOv8s (`models/yolov8s.onnx`,
  COCO class `boat`, run through OpenCV, no PyTorch). The model also runs every 2 s when
  nothing moves, because a slow tug blends into the background. Two hits start a
  crossing; the direction locks once the boat has moved 3% of the frame width; the
  crossing ends 20 s after the last hit.
- **AIS** (optional, `[ais] enabled = true`): vessel positions from aisstream.io for a
  1-mile box around the bridge (`site.bridge_lat` / `bridge_lon`, key in
  `WTH_AISSTREAM_KEY`). Each vessel gets its side of the bridge, whether it is closing on
  it and an ETA. The panel plots them on a map; `wth-edge ais` prints them in a terminal.
  A local RTL-SDR receiver can feed the same table later.

### The state machine

`wth_edge/opening.py` turns those signals into one phase, following the design doc
"Bridge opening state machine":

```
flowing -> gates_down -> lifting -> raised -> lowering -> releasing -> flowing
```

- `gates_down`: both directions at a standstill with the deck clear for 20 s. Stopped
  traffic starts the clock; a boat (camera or AIS) or a leaf rising confirms it. With
  neither for 3 minutes, or traffic moving again, it was an accident or a light and the
  phase goes back to `flowing`. A boat on its own never starts an opening: most boats in
  the old dataset pass under the closed bridge.
- `lifting` / `lowering` follow the leaf trackers; `raised` needs every visible leaf up and
  steady. A tug often passes while the near leaf is still rising, so `lifting` straight to
  `lowering` is normal.
- `releasing`: leaves flat, the queue clearing (`heavy` for a minute); `flowing` after 10 s
  of movement.
- `unknown` after 30 s with no readable signal.

Each phase maps to the website's fields with its invariants built in: `position`
(`closed` / `opening` / `open` / `closing` / `unknown`), traffic per direction (`standstill`
whenever the bridge is not closed) and `estimatedWaitTime` (only while opening, open or
closing). The wait is the median of past openings per phase, from
`<data_dir>/openings.json`, with the defaults in `[opening]` until the device has seen a
few. The console publisher logs `bridge_state` whenever those fields change; the Firestore
publisher will write them to `bridgeState/current`.

The camera sees boats approaching from the east well and from the west barely
(`site.approach_sides`), so a missing boat on the blind side never counts against an
opening; AIS covers that side.

### Auto-recording

When the phase leaves `flowing` (gates down, so the run-up is in the clip) or a boat
crossing starts, a clip starts by itself (trigger `bridge` or `boat`, with the usual 15 s
pre-roll) and stops 20 s after both are quiet. Pressing **Stop** on an automatic clip
sticks until the scene goes quiet. While any clip records, the detectors write a log into
its `clip.json` (`detections`: leaf angles and positions, lane levels with flow and fill,
deck, phase, the boat box and direction, the nearest AIS vessel, once a second, plus each
event), to use as draft labels. Events also go to the publisher.

Settings: `[detectors]`, `[bridge_detector]`, `[traffic_detector]`, `[boat_detector]`,
`[opening]`, `[ais]`, `[autorecord]` (see `wth_edge/config.py` for every knob).

### Checking the detectors against recorded clips

```sh
wth-edge --config dev/device.toml eval ~/wth-clips --label boat --out ~/wth-eval.json
```

`eval` replays every clip in a clip store through the same detector code at the live
rate and prints, per clip, the crossings and directions found against the folder labels,
the bridge positions seen and the angle range. Clips without their own ROIs (the imported
Pi 3 set) use `sites/legacy-pi3.toml`; `--site` picks another.

What it showed on the 12 old clips: the tug on 2026-04-11 was found with the right
direction and no false alarms on the two traffic clips, but 7 of the 9 "boat" clips are
small boats passing under the closed bridge, so per-clip recall is not a useful number for
that set. Only 03-18 and 04-11 hold an opening, both starting with the far leaf already up.
The bridge angle is reliable in clear light and unreliable in fog, at night and where the
old camera was zoomed differently. The state machine stays in `flowing` on every clip
(no false gates-down), but the old clips are too short to learn a lane's background, so
the traffic levels there are only indicative. Clips recorded by v2 carry their own ROIs in
`clip.json`, so `eval` on those is the real benchmark.

## Recording clips

The web panel has a **Recording** card: pick a label, add a note, press Start. Each clip
begins up to 15 s *before* you press Start (a pre-roll buffer) and stops itself after 10
minutes. Clips land in `/var/lib/wth/clips/YYYY-MM-DD/` as three files:

| File | Holds |
| --- | --- |
| `<id>.mp4` | 1280x720 H.264 from the main stream (Pi 5 encodes in software with libav) |
| `<id>.json` | When, why (`trigger`), label, note, keep flag, camera, the ROIs at the time, and (from phase 3) a per-frame detection log for draft labels |
| `<id>.jpg` | Thumbnail for the clip browser |

The **Clips** section plays, downloads, keeps (★) and deletes clips. When free disk drops
below 20%, the oldest clips that aren't kept are deleted. Settings live in `[recording]`
(`prebuffer_s`, `max_duration_s`, `bitrate`, `min_free_percent`, `labels`).

**Adjust boxes** in the live view lets you drag every box (move or resize from a corner)
and saves them to `rois.toml` on the Pi. The boxes are `bridge` (near leaf), `far_leaf`,
`boat`, `northbound` and `southbound` (the approach lanes up to their gates), and `deck`.

### Import the old dataset

The clips from `wth-inference-training/dataset` can be brought into the same layout, so
training code reads one format. Folder names become metadata (label from the top folder,
`e2w`/`w2e` direction, `bad_footage`, and `boat_details.json`). Imported clips are marked
keep. It is safe to run again.

```sh
wth-edge --config dev/device.toml import-dataset ~/wth-inference-training/dataset --data-dir ~/wth-clips --dry-run
wth-edge --config dev/device.toml import-dataset ~/wth-inference-training/dataset --data-dir ~/wth-clips --link
```

`--link` symlinks instead of copying the 8 GB of video.

## Develop on a laptop

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
wth-edge --config dev/device.toml run       # web panel on http://127.0.0.1:8080
wth-edge --config dev/device.toml display   # screen frames go to dev/display.png
pytest
```

To replay a recorded clip instead of the test pattern (it loops):

```sh
pip install -e '.[replay]'
wth-edge --config dev/device.toml run --source ~/wth-clips/clips/2026-04-03/<id>.mp4
```

Or set `driver = "file"` and `file_path` under `[camera]` in `dev/device.toml`. Recording
works on a laptop too: it encodes the 640x360 preview frames with PyAV.

## Pixel art on the round screen

Retro pixel art lives in `wth_edge/display/art/`: a small drawing kit (`kit.py`: a 26 colour
palette with neons, ASCII sprites, a 3x5 pixel font, Bayer dithering, the round mask), the
scenes drawn with it (`scenes.py`: a mallard on a rainy neon cyberpunk canal, with CRT
scanlines and VHS glitches), and a player (`player.py`). Scenes are drawn on an 80x80 canvas and
scaled 3x to the 240x240 panel. The built GIFs in `wth_edge/display/art/gifs/` are both the
previews and what the Pi plays.

| boot | idle | alert | sleep |
| --- | --- | --- | --- |
| ![boot](wth_edge/display/art/gifs/boot.gif) | ![idle](wth_edge/display/art/gifs/idle.gif) | ![alert](wth_edge/display/art/gifs/alert.gif) | ![sleep](wth_edge/display/art/gifs/sleep.gif) |

```sh
wth-edge art list                       # bundled animations
wth-edge art build [idle ...]           # redraw the GIFs after editing scenes.py
sudo systemctl stop wth-display         # the player and the status screen share the SPI bus
wth-edge play idle --seconds 30         # any bundled name, a .gif, or a folder of PNG frames
sudo systemctl start wth-display
```

### Telemetry in the art

With `display.mode = "art"` (or `"alternate"`, the default: 20 s of art, then 6 s of the text
status screen, set by `art_s` and `status_s`) the screen draws the canal scene live at
`art_fps` (8) from `state.json`, with the telemetry drawn into it (`art/live.py`):

| Shows | Means |
| --- | --- |
| ring colour | overall status: cyan ok, amber degraded, red offline, violet starting |
| bridge up or down, flashing lamps | `bridge.position`, eased while opening or closing |
| neon boat crossing | a boat is detected, in its direction (`w2e` sails left to right) |
| mallard flapping under a "!" | the bridge isn't closed or a boat is passing |
| visor glowing, or dark with static | camera ok, or camera down |
| sun yellow, orange, pulsing red | CPU temperature from 50 °C to 80 °C and up |
| skyline browning out and glitching | under-voltage right now |
| red dot by the "WTH" sign | recording |
| sleeping mallard, dim city | `night_hours` (22 to 6), unless something is happening |
| ticker on the bottom strip | one reading at a time: temperature, volts, fps, warnings, boat, bridge, REC |

| ok | boat, bridge open, recording | hot and low power |
| --- | --- | --- |
| ![ok](docs/screen/live-ok.gif) | ![boat](docs/screen/live-boat.gif) | ![hot](docs/screen/live-hot-lowpower.gif) |
| **camera down** | **night** | **offline** |
| ![camera down](docs/screen/live-camera-down.gif) | ![night](docs/screen/live-night.gif) | ![offline](docs/screen/live-offline.gif) |

`wth-edge art live-preview --out <dir>` renders these previews from the sample states in
`art/live.py`.

`wth-display.service` plays `display.boot_animation` (default `boot`, `""` for none) once
when it starts, then shows the status screen. Any GIF works with `wth-edge play`, including
ones drawn in Aseprite at 80x80 or 120x120: smaller frames are scaled up with nearest
neighbour. On a laptop, `play` writes frames to `dev/display.png`.

## Status

Done: install script, profiles, camera service with health checks, status screen,
telemetry, a console publisher, the web panel (live view, telemetry, map), manual recording
with pre-roll, the clip browser, drag-to-adjust ROIs, dataset import and replay, leaf,
traffic and boat detectors, the opening state machine with wait estimates, the AIS feed,
auto-recording with detection logs, and `eval`.

Next, per the plan: the Firestore publisher, then H.264 streaming through MediaMTX. The
bridge detector is the weak spot; once the Pi has collected labelled clips, a small
classifier on the bridge box should replace the Hough angle.
