#!/usr/bin/env bash
# Install or update the Watch The Hutch edge device on a Raspberry Pi.
#
#   sudo apt update && sudo apt install -y git
#   sudo git clone https://github.com/Jonathanferreras/wth-edge-device /opt/wth-edge
#   sudo /opt/wth-edge/install.sh --profile pi5-cam3-gc9a01 --device-id hutch-01
#
# Safe to run again at any time (that is what `sudo wth-edge update` does). It never
# prompts, and it never overwrites /etc/wth/device.toml or secrets.env unless asked.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC_DIR=/etc/wth
DEVICE_FILE="$ETC_DIR/device.toml"
SECRETS_FILE="$ETC_DIR/secrets.env"
DATA_DIR=/var/lib/wth
SERVICE_USER=wth

PROFILE=""
DEVICE_ID=""
SITE=""
RECONFIGURE=0
SKIP_APT=0
NO_START=0

usage() {
  cat <<EOF
Usage: sudo $0 [options]

  --profile NAME     hardware profile (profiles/NAME.toml). Required on first install.
  --device-id ID     name for this device (default: the hostname)
  --site NAME        calibration file (sites/NAME.toml, default: default)
  --reconfigure      rewrite $DEVICE_FILE from the options above (old one kept as .bak)
  --skip-apt         don't install system packages
  --no-start         install and enable services, but don't (re)start them
  -h, --help

Profiles: $(ls "$INSTALL_DIR/profiles" | sed 's/\.toml$//' | tr '\n' ' ')
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --site) SITE="$2"; shift 2 ;;
    --reconfigure) RECONFIGURE=1; shift ;;
    --skip-apt) SKIP_APT=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
command -v python3 >/dev/null || die "python3 is missing"

# Read a value from a TOML file: toml_get FILE dotted.key DEFAULT
toml_get() {
  python3 - "$1" "$2" "$3" <<'PY'
import sys, tomllib
path, key, default = sys.argv[1:4]
try:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    for part in key.split("."):
        data = data[part]
    print(str(data).lower() if isinstance(data, bool) else data)
except (FileNotFoundError, KeyError, TypeError):
    print(default)
PY
}

# ---------------------------------------------------------------- device file
if [[ -f "$DEVICE_FILE" && $RECONFIGURE -eq 0 ]]; then
  current_profile="$(toml_get "$DEVICE_FILE" device.profile "")"
  current_id="$(toml_get "$DEVICE_FILE" device.id "")"
  current_site="$(toml_get "$DEVICE_FILE" device.site default)"
  for pair in "PROFILE:$current_profile" "DEVICE_ID:$current_id" "SITE:$current_site"; do
    var="${pair%%:*}"; cur="${pair#*:}"
    if [[ -n "${!var}" && "${!var}" != "$cur" ]]; then
      die "$DEVICE_FILE already sets ${var,,} to '$cur'. Edit it, or pass --reconfigure to rewrite it."
    fi
  done
  PROFILE="$current_profile"; DEVICE_ID="$current_id"; SITE="$current_site"
  WRITE_DEVICE_FILE=0
else
  PROFILE="${PROFILE:-$(toml_get "$DEVICE_FILE" device.profile "")}"
  [[ -n "$PROFILE" ]] || { usage; die "--profile is required on first install"; }
  # --reconfigure keeps any value not passed on the command line.
  DEVICE_ID="${DEVICE_ID:-$(toml_get "$DEVICE_FILE" device.id "$(hostname)")}"
  SITE="${SITE:-$(toml_get "$DEVICE_FILE" device.site default)}"
  WRITE_DEVICE_FILE=1
fi

PROFILE_FILE="$INSTALL_DIR/profiles/$PROFILE.toml"
[[ -f "$PROFILE_FILE" ]] || die "no profile '$PROFILE' in $INSTALL_DIR/profiles"
[[ -f "$INSTALL_DIR/sites/$SITE.toml" ]] || die "no site '$SITE' in $INSTALL_DIR/sites"

MODEL_MATCH="$(toml_get "$PROFILE_FILE" hardware.model_match "")"
NEEDS_CAMERA="$(toml_get "$PROFILE_FILE" hardware.needs_camera false)"
NEEDS_SPI="$(toml_get "$PROFILE_FILE" hardware.needs_spi false)"
DISPLAY_DRIVER="$(toml_get "$PROFILE_FILE" display.driver none)"
SPI_BUS="$(toml_get "$PROFILE_FILE" display.spi_bus 0)"
SPI_DEV="$(toml_get "$PROFILE_FILE" display.spi_device 0)"
WEB_PORT="$(toml_get "$PROFILE_FILE" web.port 8080)"

echo "Watch The Hutch edge device"
echo "  install dir : $INSTALL_DIR"
echo "  device id   : $DEVICE_ID"
echo "  profile     : $PROFILE"
echo "  site        : $SITE"

# ---------------------------------------------------------------- board check
MODEL="$( (tr -d '\0' </proc/device-tree/model) 2>/dev/null || echo unknown)"
echo "  board       : $MODEL"
if [[ -n "$MODEL_MATCH" && "$MODEL" != *"$MODEL_MATCH"* ]]; then
  warn "profile '$PROFILE' expects '$MODEL_MATCH'; continuing anyway"
fi

# ---------------------------------------------------------------- packages
if [[ $SKIP_APT -eq 0 ]]; then
  step "Installing system packages"
  pkgs=(git python3-venv python3-pip python3-numpy python3-pil python3-psutil fonts-dejavu-core)
  if [[ "$NEEDS_CAMERA" == "true" ]]; then
    # Full rpicam-apps (not -lite) brings the libav H.264 encoder.
    # python3-av: PyavOutput writes the .mp4 clips. ffmpeg: fallback remux on older picamera2.
    # python3-opencv: the detectors (Hough lines, background subtraction, YOLO via cv2.dnn).
    # python3-websockets: the aisstream.io vessel feed ([ais] enabled = true).
    pkgs+=(python3-picamera2 rpicam-apps python3-av ffmpeg python3-opencv python3-websockets)
  fi
  if [[ "$DISPLAY_DRIVER" == "gc9a01" ]]; then
    pkgs+=(python3-spidev python3-gpiozero python3-lgpio)
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "${pkgs[@]}"
fi

# ---------------------------------------------------------------- interfaces
REBOOT_NEEDED=0
if [[ "$NEEDS_SPI" == "true" ]]; then
  step "Enabling SPI"
  if [[ -e "/dev/spidev$SPI_BUS.$SPI_DEV" ]]; then
    echo "SPI already on"
  elif command -v raspi-config >/dev/null; then
    raspi-config nonint do_spi 0
    [[ -e "/dev/spidev$SPI_BUS.$SPI_DEV" ]] || REBOOT_NEEDED=1
  else
    warn "raspi-config not found; enable SPI by hand (dtparam=spi=on in config.txt)"
  fi
fi

# ---------------------------------------------------------------- user and dirs
step "Creating service user and folders"
groups=()
for g in video gpio spi; do getent group "$g" >/dev/null && groups+=("$g"); done
group_list="$(IFS=,; echo "${groups[*]}")"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  [[ -n "$group_list" ]] && usermod -a -G "$group_list" "$SERVICE_USER"
else
  useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin \
    ${group_list:+--groups "$group_list"} "$SERVICE_USER"
fi
install -d -m 0750 -o root -g "$SERVICE_USER" "$ETC_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"

# ---------------------------------------------------------------- config
step "Writing config"
if [[ $WRITE_DEVICE_FILE -eq 1 ]]; then
  [[ -f "$DEVICE_FILE" ]] && cp -p "$DEVICE_FILE" "$DEVICE_FILE.bak" && echo "kept old file as $DEVICE_FILE.bak"
  cat >"$DEVICE_FILE" <<EOF
# This Pi's identity. Hardware comes from profiles/$PROFILE.toml and calibration from
# sites/$SITE.toml in $INSTALL_DIR. Any section here overrides those, e.g.
#   [camera]
#   framerate = 10
[device]
id = "$DEVICE_ID"
profile = "$PROFILE"
site = "$SITE"
EOF
  chown root:"$SERVICE_USER" "$DEVICE_FILE"; chmod 0640 "$DEVICE_FILE"
  echo "wrote $DEVICE_FILE"
else
  echo "keeping $DEVICE_FILE"
fi

NEW_PASSWORD=""
if [[ ! -f "$SECRETS_FILE" ]] || ! grep -q '^WTH_WEB_PASSWORD=' "$SECRETS_FILE"; then
  NEW_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  {
    [[ -f "$SECRETS_FILE" ]] || echo "# Secrets for wth-edge. Never commit this file."
    echo "WTH_WEB_PASSWORD=$NEW_PASSWORD"
  } >>"$SECRETS_FILE"
fi
chown root:"$SERVICE_USER" "$SECRETS_FILE"; chmod 0640 "$SECRETS_FILE"

# ---------------------------------------------------------------- python
step "Building the Python environment"
# --system-site-packages: picamera2, libcamera, spidev and gpiozero only come from apt.
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -e "$INSTALL_DIR"
ln -sf "$INSTALL_DIR/.venv/bin/wth-edge" /usr/local/bin/wth-edge

# ---------------------------------------------------------------- services
step "Installing systemd services"
units=(wth-edge.service)
[[ "$DISPLAY_DRIVER" != "none" ]] && units+=(wth-display.service)
for unit in "${units[@]}"; do
  sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" "$INSTALL_DIR/systemd/$unit" >"/etc/systemd/system/$unit"
  chmod 0644 "/etc/systemd/system/$unit"
done
if [[ "$DISPLAY_DRIVER" == "none" && -f /etc/systemd/system/wth-display.service ]]; then
  systemctl disable --now wth-display.service || true
  rm -f /etc/systemd/system/wth-display.service
fi
systemctl daemon-reload
systemctl enable "${units[@]}" >/dev/null
if [[ $NO_START -eq 0 && $REBOOT_NEEDED -eq 0 ]]; then
  systemctl restart "${units[@]}"
  sleep 4
fi

# ---------------------------------------------------------------- check
step "Checking"
set -a; source "$SECRETS_FILE"; set +a
WTH_CONFIG="$DEVICE_FILE" "$INSTALL_DIR/.venv/bin/wth-edge" doctor || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done."
echo "  web panel : http://$(hostname).local:$WEB_PORT  (or http://$IP:$WEB_PORT)"
if [[ -n "$NEW_PASSWORD" ]]; then
  echo "  login     : wth / $NEW_PASSWORD   (stored in $SECRETS_FILE)"
fi
echo "  logs      : journalctl -u wth-edge -f"
if [[ $REBOOT_NEEDED -eq 1 ]]; then
  echo
  echo "SPI was just enabled. Reboot to finish: sudo reboot"
fi
