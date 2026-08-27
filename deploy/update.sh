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
cd "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/deploy/start-camera.sh"
git config --global --add safe.directory "$INSTALL_DIR"

if ! command -v mjpg_streamer >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y mjpg-streamer v4l-utils
fi
if [[ -f /etc/astrodrive/astrodrive.env ]]; then
  sed -i 's|^CAMERA_URL=http://localhost:8080/|CAMERA_URL=/camera/?action=stream|' /etc/astrodrive/astrodrive.env
fi

progress 1 5 "Checking for source updates"
previous_revision="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ "${1:-}" != "--skip-fetch" ]]; then
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

install -m 0644 "$INSTALL_DIR/deploy/astrodrive-api.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-update.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-update.timer" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-camera.service" /etc/systemd/system/
if command -v nginx >/dev/null 2>&1; then
  install -m 0644 "$INSTALL_DIR/deploy/astrodrive.nginx" /etc/nginx/sites-available/astrodrive
  ln -sfn /etc/nginx/sites-available/astrodrive /etc/nginx/sites-enabled/astrodrive
  rm -f /etc/nginx/sites-enabled/default
fi
systemctl daemon-reload
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
if [[ "${ESP32_AUTO_FLASH:-true}" == true && "$(cat "$FIRMWARE_MARKER" 2>/dev/null || true)" != "$(git rev-parse HEAD)" ]]; then
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
  backend/api/.venv/bin/pip install --quiet platformio
  upload_args=()
  if [[ "${ESP32_SERIAL_PORT:-auto}" != "auto" ]]; then
    upload_args+=(--upload-port "$ESP32_SERIAL_PORT")
  else
    for candidate in /dev/ttyUSB* /dev/ttyACM*; do
      if [[ -e "$candidate" ]]; then
        upload_args+=(--upload-port "$candidate")
        break
      fi
    done
  fi
  backend/api/.venv/bin/pio run -d esp32/firmware
  if [[ ${#upload_args[@]} -eq 0 ]]; then
    echo "ESP32 not connected; firmware was built but not uploaded."
  elif ! backend/api/.venv/bin/pio run -d esp32/firmware -t upload "${upload_args[@]}"; then
    echo "ESP32 firmware upload was not completed; continuing with Pi services."
  else
    printf '%s\n' "$(git rev-parse HEAD)" > "$FIRMWARE_MARKER"
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
  nginx -t && systemctl reload nginx || true
fi
