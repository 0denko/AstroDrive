#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${1:-${ASTRODRIVE_REPO_URL:-https://github.com/0denko/AstroDrive}}"
INSTALL_DIR="${ASTRODRIVE_INSTALL_DIR:-/opt/astrodrive}"
BRANCH="${ASTRODRIVE_BRANCH:-main}"
SERVICE_USER="astrodrive"

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv python3-pip nginx nodejs npm
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
usermod -aG dialout "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR" /etc/astrodrive /var/lib/astrodrive /var/lib/astrodrive/frontend

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  rm -rf "$INSTALL_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
fi

if [[ ! -f /etc/astrodrive/astrodrive.env ]]; then
  install -o root -g "$SERVICE_USER" -m 0640 "$INSTALL_DIR/deploy/astrodrive.env.example" /etc/astrodrive/astrodrive.env
fi
sed -i "s|^ASTRODRIVE_REPO_URL=.*|ASTRODRIVE_REPO_URL=$REPO_URL|; s|^ASTRODRIVE_BRANCH=.*|ASTRODRIVE_BRANCH=$BRANCH|; s|^ASTRODRIVE_INSTALL_DIR=.*|ASTRODRIVE_INSTALL_DIR=$INSTALL_DIR|" /etc/astrodrive/astrodrive.env

chmod +x "$INSTALL_DIR/deploy/update.sh"
"$INSTALL_DIR/deploy/update.sh" --skip-fetch
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-api.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-update.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive-update.timer" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/deploy/astrodrive.nginx" /etc/nginx/sites-available/astrodrive
ln -sfn /etc/nginx/sites-available/astrodrive /etc/nginx/sites-enabled/astrodrive
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now astrodrive-api.service
systemctl enable --now astrodrive-update.timer
systemctl reload nginx || systemctl restart nginx

echo "AstroDrive is installed. Open http://$(hostname -I | awk '{print $1}')"
