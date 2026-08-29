#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="/etc/astrodrive/astrodrive.env"
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE"
INSTALL_DIR="${ASTRODRIVE_INSTALL_DIR:-/opt/astrodrive}"
BRANCH="${ASTRODRIVE_BRANCH:-main}"
SERVICE_USER="astrodrive"
FIRMWARE_MARKER="/var/lib/astrodrive/firmware-revision"

progress() {
  local current="$1" total="$2" label="$3" width=28 filled
  if [[ "${ASTRODRIVE_NESTED:-false}" == true ]]; then
    return
  fi
  filled=$((current * width / total))
  printf "\n[%3d%%] [" $((current * 100 / total))
  printf "%*s" "$filled" "" | tr ' ' '#'
  printf "%*s] %s\n" $((width - filled)) "" "$label"
}

if [[ $EUID -ne 0 ]]; then
  echo "Run the updater as root."
  exit 1
fi

exec 9>/run/lock/astrodrive-update.lock
if ! flock -n 9; then
  echo "Another AstroDrive update is already running; skipping this run."
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y git python3 python3-venv python3-pip nginx nodejs npm v4l-utils build-essential cmake libjpeg-dev
apt-get install -y imagemagick python3-pil python3-numpy
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
usermod -aG dialout "$SERVICE_USER"
usermod -aG video "$SERVICE_USER"
# the API reads the updater's journal to report live progress
usermod -aG systemd-journal "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR" /etc/astrodrive /var/lib/astrodrive /var/lib/astrodrive/frontend
cd "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/deploy/start-camera.sh"
chmod +x "$INSTALL_DIR/deploy/stack-camera.py"
git config --global --add safe.directory "$INSTALL_DIR"

progress 1 5 "Checking for source updates"
previous_revision="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ "${1:-}" != "--skip-fetch" ]]; then
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

if ! command -v mjpg_streamer >/dev/null 2>&1 && [[ ! -x /opt/mjpg-streamer/bin/mjpg_streamer && ! -x /opt/mjpg-streamer/mjpg_streamer ]]; then
  apt-get install -y v4l-utils build-essential cmake libjpeg-dev
  rm -rf /tmp/mjpg-streamer
  git clone --depth 1 https://github.com/jacksonliam/mjpg-streamer.git /tmp/mjpg-streamer
  cmake -S /tmp/mjpg-streamer/mjpg-streamer-experimental -B /tmp/mjpg-streamer/build
  cmake --build /tmp/mjpg-streamer/build -j2
  cmake --install /tmp/mjpg-streamer/build --prefix /opt/mjpg-streamer
  rm -rf /tmp/mjpg-streamer
fi
if [[ -f /etc/astrodrive/astrodrive.env ]]; then
  sed -i 's|^CAMERA_URL=http://localhost:8080/|CAMERA_URL=/camera/?action=stream|' /etc/astrodrive/astrodrive.env
  sed -i 's|^CAMERA_URL=/camera/?action=stream?action=stream$|CAMERA_URL=/camera/?action=stream|' /etc/astrodrive/astrodrive.env
fi

install -m 0644 "$INSTALL_DIR/deploy/astrodrive-api.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-update.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-update.timer" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-camera.service" /etc/systemd/system/
install -m 0440 "$INSTALL_DIR/deploy/astrodrive-update.sudoers" /etc/sudoers.d/astrodrive-update
if command -v nginx >/dev/null 2>&1; then
  install -m 0644 "$INSTALL_DIR/deploy/astrodrive.nginx" /etc/nginx/sites-available/astrodrive
  ln -sfn /etc/nginx/sites-available/astrodrive /etc/nginx/sites-enabled/astrodrive
  rm -f /etc/nginx/sites-enabled/default
fi
systemctl daemon-reload
visudo -cf /etc/sudoers.d/astrodrive-update
systemctl enable astrodrive-api.service astrodrive-camera.service astrodrive-update.service astrodrive-update.timer >/dev/null
firmware_changed=false
if [[ "${1:-}" == "--skip-fetch" ]] || [[ -z "$previous_revision" ]] || ! git diff --quiet "$previous_revision" HEAD -- esp32; then
  firmware_changed=true
fi
backend_changed=false
if [[ "${1:-}" == "--skip-fetch" ]] || [[ -z "$previous_revision" ]] || ! git diff --quiet "$previous_revision" HEAD -- backend; then
  backend_changed=true
fi
frontend_changed=false
if [[ "${1:-}" == "--skip-fetch" ]] || [[ -z "$previous_revision" ]] || ! git diff --quiet "$previous_revision" HEAD -- frontend; then
  frontend_changed=true
fi
firmware_upload_needed=false
board_env="${ESP32_BOARD_ENV:-esp32dev}"
board_identity=""
if [[ "$board_env" == auto ]]; then
  # the USB bridge is a CH340 on most of these boards, so its ids cannot name the MCU. They only
  # say "something different is plugged in", which is enough to trigger a fresh detection, and it
  # reads from sysfs so it does not disturb the port the API is holding open
  for candidate in /dev/ttyUSB* /dev/ttyACM*; do
    [[ -e "$candidate" ]] || continue
    board_identity+="$(udevadm info -q property -n "$candidate" 2>/dev/null | grep -E '^(ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT)=' | sort | tr '\n' ',')"
  done
  board_identity=" ${board_identity:-none}"
fi
# the marker carries the board too, so switching between an ESP32 and a NodeMCU reflashes
firmware_stamp="$(git rev-parse HEAD) $board_env$board_identity"
if [[ "${ESP32_AUTO_FLASH:-true}" == true && "$(cat "$FIRMWARE_MARKER" 2>/dev/null || true)" != "$firmware_stamp" ]]; then
  firmware_upload_needed=true
fi
if [[ "${1:-}" != "--skip-fetch" && "$previous_revision" == "$(git rev-parse HEAD)" && "$firmware_upload_needed" != true && "$backend_changed" != true && "$frontend_changed" != true ]]; then
  echo "AstroDrive is already up to date; deployment registration is current."
  exit 0
fi

if [[ "$backend_changed" == true || ! -x backend/api/.venv/bin/python ]]; then
  progress 2 5 "Installing backend dependencies"
  python3 -m venv backend/api/.venv
  backend/api/.venv/bin/pip install --quiet --upgrade pip
  backend/api/.venv/bin/pip install --quiet -r backend/api/requirements.txt
else
  progress 2 5 "Backend is already current"
fi
if [[ ("$firmware_changed" == true || "$firmware_upload_needed" == true) && "${ESP32_AUTO_FLASH:-true}" == true ]]; then
  progress 3 5 "Building and uploading ESP32 firmware"
  backend/api/.venv/bin/pip install --quiet platformio esptool
  if [[ "$board_env" == auto ]]; then
    # only the ROM can name the MCU, and the API holds the port for its whole uptime, so borrow it
    # for the few seconds the query takes rather than for the whole build
    systemctl stop astrodrive-api.service || true
    trap 'systemctl start astrodrive-api.service || true' EXIT
    detect_port=""
    for candidate in /dev/ttyUSB* /dev/ttyACM*; do
      [[ -e "$candidate" ]] || continue
      detect_port="$candidate"
      break
    done
    detected_chip=""
    if [[ -n "$detect_port" ]]; then
      detected_chip="$(timeout 30 backend/api/.venv/bin/python -m esptool --port "$detect_port" chip_id 2>/dev/null | grep -m1 'Chip is' || true)"
    fi
    trap - EXIT
    systemctl start astrodrive-api.service || true
    case "$detected_chip" in
      *ESP8266*) board_env=nodemcuv2 ;;
      # no environment exists for these yet, and flashing the wrong one is worse than not flashing
      *ESP32-S*|*ESP32-C*|*ESP32-H*) board_env="" ;;
      *ESP32*) board_env=esp32dev ;;
      *) board_env="" ;;
    esac
    if [[ -z "$board_env" ]]; then
      echo "WARNING: could not identify the attached board (${detected_chip:-no response on ${detect_port:-no port}}); skipping the firmware flash."
    else
      echo "Detected ${detected_chip}; building environment $board_env."
    fi
  fi
  build_ok=true
  if [[ -z "$board_env" ]]; then
    build_ok=false
  else
    backend/api/.venv/bin/pio run -d esp32/firmware -e "$board_env" || build_ok=false
  fi
  if [[ "$build_ok" != true ]]; then
    echo "WARNING: ESP32 firmware build failed for board ${board_env:-unknown}; the board still runs its previous firmware."
  else
    # esptool needs the port to itself, and the API holds it open for the whole of its uptime
    systemctl stop astrodrive-api.service || true
    # the API is the only remote control there is, so it has to come back even if this step dies
    trap 'systemctl start astrodrive-api.service || true' EXIT
    upload_args=()
    if [[ "${ESP32_SERIAL_PORT:-auto}" != "auto" ]]; then
      upload_args+=(--upload-port "$ESP32_SERIAL_PORT")
    else
      for candidate in /dev/ttyUSB* /dev/ttyACM*; do
        [[ -e "$candidate" ]] || continue
        # existing is not the same as openable; a dead adapter would otherwise win the race
        if backend/api/.venv/bin/python -c "import serial,sys; serial.Serial(sys.argv[1]).close()" "$candidate" 2>/dev/null; then
          upload_args+=(--upload-port "$candidate")
          break
        fi
      done
    fi
    if [[ ${#upload_args[@]} -eq 0 ]]; then
      echo "WARNING: ESP32 not connected; firmware was built but not uploaded."
    elif timeout 240 backend/api/.venv/bin/pio run -d esp32/firmware -e "$board_env" -t upload "${upload_args[@]}"; then
      printf '%s\n' "$firmware_stamp" > "$FIRMWARE_MARKER"
    else
      # a failed flash is recorded too, or the timer would stop the API every quarter of an hour.
      # That also means nothing here will retry it, so the message has to say how
      printf '%s\n' "$firmware_stamp" > "$FIRMWARE_MARKER"
      echo "WARNING: ESP32 firmware upload FAILED for board $board_env; the board still runs its previous firmware. Retry with: sudo rm $FIRMWARE_MARKER"
    fi
    trap - EXIT
    systemctl start astrodrive-api.service || true
  fi
fi
if [[ "$firmware_changed" != true && "$firmware_upload_needed" != true || "${ESP32_AUTO_FLASH:-true}" != true ]]; then
  progress 3 5 "ESP32 firmware is already current"
fi
if [[ "$frontend_changed" == true || ! -d frontend/ui/dist ]]; then
  progress 4 5 "Building web interface"
  cd frontend/ui
  npm install --no-audit --no-fund
  npm run build
  cd "$INSTALL_DIR"
else
  progress 4 5 "Web interface is already current"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/astrodrive/frontend
if [[ "$frontend_changed" == true || ! -f /var/lib/astrodrive/frontend/index.html ]]; then
  rm -rf /var/lib/astrodrive/frontend/*
  cp -a frontend/ui/dist/. /var/lib/astrodrive/frontend/
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" /var/lib/astrodrive/frontend
progress 5 5 "Restarting services"
systemctl try-restart --no-block astrodrive-api.service || true
systemctl try-restart --no-block astrodrive-camera.service || true
if command -v nginx >/dev/null 2>&1 && [[ "${ASTRODRIVE_NESTED:-false}" != true ]]; then
  # nginx -t is chatty on success, and that output would become the status line the UI shows
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx || true
  else
    nginx -t || true
  fi
fi
echo "Update finished"
