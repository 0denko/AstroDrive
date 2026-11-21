#!/usr/bin/env bash
# AstroDrive One-Line Installer for Raspberry Pi (Astroberry OS v3.2+ or Raspberry Pi OS 64-bit)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/0denko/AstroDrive/main/setup.sh | sudo bash
#
# What this does automatically:
#   1. Installs required packages (python3, venv, git, curl, udev).
#   2. Downloads (or updates) AstroDrive into /opt/astrodrive-src.
#   3. Installs the OnStepX auto-flasher, Web Setup Wizard, and Astroberry UI overlay.
#   4. Pre-downloads the latest verified ESP32 & ESP8266 OnStepX firmware from GitHub Releases.
#   5. Pre-configures KStars/Ekos INDI defaults (/dev/onstep @ 9600 baud).
set -euo pipefail

REPO_URL="${ASTRODRIVE_REPO_URL:-https://github.com/0denko/AstroDrive.git}"
GITHUB_REPO="${ASTRODRIVE_GITHUB_REPO:-0denko/AstroDrive}"
SRC_DIR="${ASTRODRIVE_SRC_DIR:-/opt/astrodrive-src}"

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -E bash "$0" "$@"
  fi
  echo "[ERROR] Please run with sudo: curl -fsSL https://raw.githubusercontent.com/0denko/AstroDrive/main/setup.sh | sudo bash" >&2
  exit 1
fi

echo ""
echo "============================================================"
echo "  AstroDrive Automatic Setup"
echo "============================================================"
echo ""

echo "==> [1/4] Installing required system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git curl udev

echo "==> [2/4] Downloading AstroDrive..."
if [ -d "${SRC_DIR}/.git" ]; then
  git -C "${SRC_DIR}" fetch --depth 1 origin main
  git -C "${SRC_DIR}" reset --hard origin/main
else
  rm -rf "${SRC_DIR}"
  git clone --depth 1 "${REPO_URL}" "${SRC_DIR}"
fi

echo "==> [3/4] Installing AstroDrive services & Web Setup Wizard..."
chmod +x "${SRC_DIR}/install.sh"
"${SRC_DIR}/install.sh" --enable-auto-flash --github-repo "${GITHUB_REPO}" "$@"

echo "==> [4/4] Pre-downloading latest verified OnStepX firmware (ESP32 + ESP8266)..."
/opt/onstep-autoprovision/scripts/update_firmware.sh --all || true

HOST_NAME="$(hostname 2>/dev/null || echo astroberry)"
echo ""
echo "============================================================"
echo "  AstroDrive is installed and ready!"
echo ""
echo "  Next: Open your browser and go to:"
echo "    -> http://astroberry.local/onstep/   (Astroberry OS)"
echo "    -> http://${HOST_NAME}.local:8090/   (Standalone fallback)"
echo ""
echo "  Follow the on-screen Setup Wizard (Click Next -> Next -> Done)!"
echo "============================================================"
echo ""
