#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="/etc/astrodrive/astrodrive.env"
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE"
INSTALL_DIR="${ASTRODRIVE_INSTALL_DIR:-/opt/astrodrive}"
BRANCH="${ASTRODRIVE_BRANCH:-main}"
SERVICE_USER="astrodrive"

if [[ $EUID -ne 0 ]]; then
  echo "Run the updater as root."
  exit 1
fi
cd "$INSTALL_DIR"

previous_revision="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ "${1:-}" != "--skip-fetch" ]]; then
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi
firmware_changed=false
if [[ "${1:-}" == "--skip-fetch" ]] || [[ -z "$previous_revision" ]] || ! git diff --quiet "$previous_revision" HEAD -- esp32; then
  firmware_changed=true
fi

python3 -m venv backend/api/.venv
backend/api/.venv/bin/pip install --quiet --upgrade pip
backend/api/.venv/bin/pip install --quiet -r backend/api/requirements.txt
if [[ "$firmware_changed" == true && "${ESP32_AUTO_FLASH:-true}" == true ]]; then
  backend/api/.venv/bin/pip install --quiet platformio
  upload_args=()
  if [[ "${ESP32_SERIAL_PORT:-auto}" != "auto" ]]; then
    upload_args+=(--upload-port "$ESP32_SERIAL_PORT")
  fi
  if ! backend/api/.venv/bin/pio run -d esp32/firmware -t upload "${upload_args[@]}"; then
    echo "ESP32 firmware upload was not completed; continuing with Pi services."
  fi
fi
cd frontend/ui
npm install --no-audit --no-fund
npm run build
cd "$INSTALL_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/astrodrive/frontend
rm -rf /var/lib/astrodrive/frontend/*
cp -a frontend/ui/dist/. /var/lib/astrodrive/frontend/
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" /var/lib/astrodrive/frontend
systemctl try-restart astrodrive-api.service || true
systemctl reload nginx || true
