#!/usr/bin/env bash
# Installer for astro-pi-onstep-autoprovision on Raspberry Pi OS (Bookworm/Trixie)
# and Astroberry OS v3.x (64-bit arm64).
#
# Flags:
#   --with-arduino-cli   Install arduino-cli + pinned ESP8266 (3.1.2) & ESP32 (2.0.17) cores
#                        so the Pi can pull latest OnStepX and live-compile on-device!
#   --enable-auto-flash  Set AUTO_FLASH="true" in /etc/onstep-autoprovision/config.env
#   --github-repo O/R    Pre-configure GITHUB_REPO="owner/repo" for online firmware fetches
#   --chroot             Chroot-safe mode (used by .github/workflows/build-pi-image.yml;
#                        skips live systemctl/udevadm daemon reloads)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Please run with sudo: sudo ./install.sh" >&2
  exit 1
fi

WITH_ARDUINO_CLI="false"
ENABLE_AUTO_FLASH="false"
CHROOT_MODE="false"
GITHUB_REPO_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-arduino-cli) WITH_ARDUINO_CLI="true"; shift ;;
    --enable-auto-flash) ENABLE_AUTO_FLASH="true"; shift ;;
    --github-repo) GITHUB_REPO_ARG="${2:?Missing owner/repo}"; shift 2 ;;
    --chroot) CHROOT_MODE="true"; shift ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# Auto-detect chroot even if --chroot wasn't passed explicitly
if command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --chroot >/dev/null 2>&1; then
  CHROOT_MODE="true"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/onstep-autoprovision"
CONFIG_DIR="/etc/onstep-autoprovision"
CONFIG_FILE="${CONFIG_DIR}/config.env"
ARDUINO_CACHE="/var/cache/onstep-arduino"

echo "==> Installing scripts, Web UI overlay & firmware profiles to ${INSTALL_DIR}..."
mkdir -p \
  "${INSTALL_DIR}/scripts" \
  "${INSTALL_DIR}/webui" \
  "${INSTALL_DIR}/firmware" \
  "${INSTALL_DIR}/firmware-src" \
  "${CONFIG_DIR}" \
  "${ARDUINO_CACHE}/staging" \
  "${ARDUINO_CACHE}/user"

install -m 0755 "${REPO_DIR}/scripts/onstep_provision.py" "${INSTALL_DIR}/scripts/onstep_provision.py"
install -m 0755 "${REPO_DIR}/scripts/probe_onstep.py" "${INSTALL_DIR}/scripts/probe_onstep.py"
install -m 0755 "${REPO_DIR}/scripts/update_firmware.sh" "${INSTALL_DIR}/scripts/update_firmware.sh"
install -m 0755 "${REPO_DIR}/scripts/setup_hotspot.sh" "${INSTALL_DIR}/scripts/setup_hotspot.sh"
install -m 0755 "${REPO_DIR}/scripts/configure_nginx_overlay.sh" "${INSTALL_DIR}/scripts/configure_nginx_overlay.sh"
install -m 0755 "${REPO_DIR}/webui/onstep_webui.py" "${INSTALL_DIR}/webui/onstep_webui.py"
install -m 0644 "${REPO_DIR}/webui/index.html" "${INSTALL_DIR}/webui/index.html"
install -m 0644 "${REPO_DIR}/webui/overlay.js" "${INSTALL_DIR}/webui/overlay.js"
install -m 0644 "${REPO_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"

# Copy firmware overlay profiles & build.sh so the Pi can compile live when online
install -m 0755 "${REPO_DIR}/firmware/build.sh" "${INSTALL_DIR}/firmware-src/build.sh"
rm -rf "${INSTALL_DIR}/firmware-src/profiles"
cp -r "${REPO_DIR}/firmware/profiles" "${INSTALL_DIR}/firmware-src/profiles"

# Copy any prebuilt firmware bundles (<profile>/*.bin + manifest.json)
if [ -d "${REPO_DIR}/firmware/prebuilt" ]; then
  for prof_dir in "${REPO_DIR}/firmware/prebuilt/"*; do
    if [ -d "${prof_dir}" ] && [ -f "${prof_dir}/manifest.json" ]; then
      prof_name="$(basename "${prof_dir}")"
      echo "==> Installing prebuilt firmware bundle for profile '${prof_name}'..."
      mkdir -p "${INSTALL_DIR}/firmware/${prof_name}"
      cp -v "${prof_dir}/"*.bin "${INSTALL_DIR}/firmware/${prof_name}/" 2>/dev/null || true
      cp -v "${prof_dir}/manifest.json" "${INSTALL_DIR}/firmware/${prof_name}/manifest.json"
    fi
  done
fi

echo "==> Creating Python virtual environment (${INSTALL_DIR}/.venv)..."
if [ ! -d "${INSTALL_DIR}/.venv" ]; then
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

if [ "${WITH_ARDUINO_CLI}" = "true" ]; then
  echo "==> Installing arduino-cli and pinned ESP8266/ESP32 cores for on-Pi live compilation..."
  if ! command -v arduino-cli >/dev/null 2>&1; then
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh
  fi
  export ARDUINO_DIRECTORIES_DATA="${ARDUINO_CACHE}"
  export ARDUINO_DIRECTORIES_DOWNLOADS="${ARDUINO_CACHE}/staging"
  export ARDUINO_DIRECTORIES_USER="${ARDUINO_CACHE}/user"
  arduino-cli config init --overwrite >/dev/null 2>&1 || true
  arduino-cli config add board_manager.additional_urls \
    "https://arduino.esp8266.com/stable/package_esp8266com_index.json" \
    "https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json" >/dev/null
  arduino-cli core update-index
  arduino-cli core install esp8266:esp8266@3.1.2
  arduino-cli core install esp32:esp32@2.0.17
fi

if [ ! -f "${CONFIG_FILE}" ]; then
  echo "==> Creating default configuration at ${CONFIG_FILE}..."
  install -m 0644 "${REPO_DIR}/config/onstep-autoprovision.env.example" "${CONFIG_FILE}"
else
  echo "==> Preserving existing configuration at ${CONFIG_FILE}."
fi

if [ "${ENABLE_AUTO_FLASH}" = "true" ]; then
  sed -i 's/^AUTO_FLASH=.*/AUTO_FLASH="true"/' "${CONFIG_FILE}"
fi

if [ -n "${GITHUB_REPO_ARG}" ]; then
  sed -i "s|^GITHUB_REPO=.*|GITHUB_REPO=\"${GITHUB_REPO_ARG}\"|" "${CONFIG_FILE}"
fi

echo "==> Installing udev rule and systemd units..."
install -m 0644 "${REPO_DIR}/udev/99-onstep-nodemcu.rules" /etc/udev/rules.d/99-onstep-nodemcu.rules
install -m 0644 "${REPO_DIR}/systemd/onstep-autoprovision@.service" /etc/systemd/system/onstep-autoprovision@.service
install -m 0644 "${REPO_DIR}/systemd/onstep-firmware-update.service" /etc/systemd/system/onstep-firmware-update.service
install -m 0644 "${REPO_DIR}/systemd/onstep-firmware-update.timer" /etc/systemd/system/onstep-firmware-update.timer
install -m 0644 "${REPO_DIR}/systemd/onstep-webui.service" /etc/systemd/system/onstep-webui.service

# Enable background firmware refresh timer & Astroberry Web UI overlay service
systemctl enable onstep-firmware-update.timer >/dev/null 2>&1 || true
systemctl enable onstep-webui.service >/dev/null 2>&1 || true

# Install Nginx Astroberry UI overlay + APT post-invoke hook (preserves overlay after `apt upgrade`)
if [ -d /etc/nginx ]; then
  mkdir -p /etc/nginx/snippets
  install -m 0644 "${REPO_DIR}/nginx/onstep-astroberry-overlay.conf" /etc/nginx/snippets/onstep-astroberry-overlay.conf
  "${INSTALL_DIR}/scripts/configure_nginx_overlay.sh" || true
  if [ -d /etc/apt/apt.conf.d ]; then
    cat > /etc/apt/apt.conf.d/99-onstep-nginx-overlay <<'EOF'
DPkg::Post-Invoke { "if [ -x /opt/onstep-autoprovision/scripts/configure_nginx_overlay.sh ]; then /opt/onstep-autoprovision/scripts/configure_nginx_overlay.sh || true; fi"; };
EOF
  fi
fi

# Disable gpsd USBAUTO so it cannot hijack /dev/ttyUSB* from OnStep/INDI
if [ -f /etc/default/gpsd ] && grep -Eq '^\s*USBAUTO\s*=\s*"?true"?' /etc/default/gpsd; then
  echo "==> Disabling USBAUTO in /etc/default/gpsd to prevent serial-port hijacking..."
  sed -i 's/^\s*USBAUTO=.*/USBAUTO="false"/' /etc/default/gpsd
fi

if [ "${CHROOT_MODE}" = "false" ]; then
  systemctl daemon-reload
  systemctl start onstep-firmware-update.timer || true
  systemctl restart onstep-webui.service || true
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=tty

  if [ -n "${SUDO_USER:-}" ] && id "${SUDO_USER}" >/dev/null 2>&1; then
    usermod -aG dialout "${SUDO_USER}"
    echo "==> Added ${SUDO_USER} to 'dialout' group (log out/in to apply)."
  fi
else
  echo "==> Chroot mode detected: skipped live daemon reloads (will activate on first boot)."
  # Ensure default Astroberry/Pi users are in dialout inside chroot
  for u in astroberry pi; do
    if id "${u}" >/dev/null 2>&1; then
      usermod -aG dialout "${u}" || true
    fi
  done
fi

echo ""
echo "==> Installation complete!"
echo "Next steps:"
echo "  1. Open http://astroberry.local (OnStepX pill/drawer overlay) or http://$(hostname).local:8090"
echo "  2. Review ${CONFIG_FILE} (TARGET_CHIP=auto, GITHUB_REPO, AUTO_FLASH=true)."
echo "  3. Optional dark-sky Wi-Fi fallback: sudo ${INSTALL_DIR}/scripts/setup_hotspot.sh --ap-ssid AstroPi-OnStep --ap-pass <8+chars>"
echo "  4. Plug in any NodeMCU (ESP8266 or ESP32) and watch live progress in the Web UI or: journalctl -fu 'onstep-autoprovision@*'"
echo "  5. Configure KStars/Ekos driver 'LX200 OnStep' (indi_lx200_OnStep) on /dev/onstep @ 9600 baud."
