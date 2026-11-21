#!/usr/bin/env bash
# AstroDrive One-Line Installer for Raspberry Pi (Astroberry OS v3.2+ or Raspberry Pi OS 64-bit)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/0denko/AstroDrive/main/setup.sh | sudo bash
#
# What this does automatically:
#   1. Installs required packages if missing (python3, venv, git, curl, udev).
#   2. Downloads (or updates) AstroDrive into /opt/astrodrive-src.
#   3. Installs the OnStepX provisioner (safe AUTO_FLASH=false by default),
#      authenticated Web Setup Wizard, daily self-updater, and Astroberry UI overlay.
#   4. Downloads the latest CI-built ESP32 & ESP8266 OnStepX firmware (SHA-256 checked
#      against the release manifest) and warns clearly if offline or download fails.
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

# #15: Skip apt-get update/install if all required packages are already installed
REQUIRED_PKGS=(python3 python3-venv python3-pip git curl udev)
MISSING_PKGS=()
for pkg in "${REQUIRED_PKGS[@]}"; do
  if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
    MISSING_PKGS+=("${pkg}")
  fi
done
if [ "${#MISSING_PKGS[@]}" -eq 0 ]; then
  echo "==> [1/4] Required system packages already installed (skipping apt)."
else
  echo "==> [1/4] Installing missing system packages: ${MISSING_PKGS[*]}..."
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING_PKGS[@]}"
fi

echo "==> [2/4] Downloading AstroDrive..."
if [ -d "${SRC_DIR}/.git" ]; then
  echo "==> Updating existing ${SRC_DIR} to latest origin/main (any local edits in ${SRC_DIR} will be discarded)..."
  git -C "${SRC_DIR}" fetch --depth 1 origin main
  git -C "${SRC_DIR}" reset --hard origin/main
else
  rm -rf "${SRC_DIR}"
  git clone --depth 1 "${REPO_URL}" "${SRC_DIR}"
fi

echo "==> [3/4] Installing AstroDrive services & Web Setup Wizard..."
chmod +x "${SRC_DIR}/install.sh"
"${SRC_DIR}/install.sh" --github-repo "${GITHUB_REPO}" "$@"

echo "==> [4/4] Downloading latest CI-built OnStepX firmware (ESP32 + ESP8266)..."
FW_OK="true"
if ! /opt/onstep-autoprovision/scripts/update_firmware.sh --all; then
  FW_OK="false"
fi

# Verify at least one .bin + manifest.json is actually cached on disk (M2)
CACHED_BINS="$(find /opt/onstep-autoprovision/firmware -maxdepth 2 -name '*.bin' 2>/dev/null | wc -l | tr -d ' ')"
if [ "${CACHED_BINS:-0}" -eq 0 ]; then
  FW_OK="false"
fi

HOST_NAME="$(hostname 2>/dev/null || echo astroberry)"
WEBUI_TOKEN="$(tr -d '[:space:]' < /etc/onstep-autoprovision/webui.token 2>/dev/null || echo "")"
OVERLAY_ATTACHED="$(python3 -c 'import json; print(json.load(open("/var/lib/onstep-autoprovision/install-status.json")).get("nginx_overlay", ""))' 2>/dev/null || echo "")"

echo ""
echo "============================================================"
if [ "${FW_OK}" = "true" ]; then
  echo "  AstroDrive is installed and ready!"
else
  echo "  [WARNING] AstroDrive installed, BUT firmware download FAILED!"
  echo "  No OnStepX firmware binaries are cached on this Pi yet."
  echo "  -> Connect the Pi to the internet and re-run setup.sh, OR"
  echo "     click 'Download Firmware' on Step 1 of the Setup Wizard."
fi
echo ""
echo "  Open the Setup Wizard in your browser (uses #token fragment, never logged by Nginx):"
if [ "${OVERLAY_ATTACHED}" = "attached" ]; then
  echo "    -> http://astroberry.local/onstep/#token=${WEBUI_TOKEN}"
else
  echo "    (Astroberry Nginx overlay was not attached — use the direct URL below)"
fi
echo "    -> http://${HOST_NAME}.local:8090/#token=${WEBUI_TOKEN}"
echo ""
echo "  Your Setup PIN (if asked): ${WEBUI_TOKEN}"
echo "  (Saved in /etc/onstep-autoprovision/webui.token)"
echo "============================================================"
echo ""
