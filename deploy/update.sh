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

if [[ "${1:-}" != "--skip-fetch" ]]; then
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

python3 -m venv backend/api/.venv
backend/api/.venv/bin/pip install --quiet --upgrade pip
backend/api/.venv/bin/pip install --quiet -r backend/api/requirements.txt
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
