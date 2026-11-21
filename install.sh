#!/usr/bin/env bash
# Installer for AstroDrive on Raspberry Pi OS (Bookworm/Trixie)
# and Astroberry OS v3.x (64-bit arm64).
#
# Flags:
#   --with-arduino-cli   Install pinned arduino-cli (v1.6.0, SHA-256 verified) +
#                        ESP8266 (3.1.2) & ESP32 (2.0.17) cores for on-Pi compilation
#   --enable-auto-flash  Set AUTO_FLASH="true" in /etc/onstep-autoprovision/config.env
#                        (default is "false" for safety; Wizard offers 1-click flash)
#   --github-repo O/R    Pre-configure GITHUB_REPO="owner/repo" for online updates
#   --skip-udev-trigger  Skip `udevadm trigger` (used during background self-updates)
#   --chroot             Chroot-safe mode (used by .github/workflows/build-pi-image.yml)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Please run with sudo: sudo ./install.sh" >&2
  exit 1
fi

WITH_ARDUINO_CLI="false"
ENABLE_AUTO_FLASH="false"
SKIP_UDEV_TRIGGER="false"
CHROOT_MODE="false"
GITHUB_REPO_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-arduino-cli) WITH_ARDUINO_CLI="true"; shift ;;
    --enable-auto-flash) ENABLE_AUTO_FLASH="true"; shift ;;
    --skip-udev-trigger) SKIP_UDEV_TRIGGER="true"; shift ;;
    --github-repo) GITHUB_REPO_ARG="${2:?Missing owner/repo}"; shift 2 ;;
    --chroot) CHROOT_MODE="true"; shift ;;
    -h|--help)
      sed -n '2,13p' "$0"
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
META_FILE="${CONFIG_DIR}/install-meta.env"
TOKEN_FILE="${CONFIG_DIR}/webui.token"
STATE_DIR="/var/lib/onstep-autoprovision"
ARDUINO_CACHE="/var/cache/onstep-arduino"
COMMON_PY="${INSTALL_DIR}/scripts/onstep_common.py"

# Helper (m1 / #12): update KEY="VALUE" if present, or append if missing, via shared onstep_common.py
set_config_key() {
  python3 "${COMMON_PY}" set-config "${CONFIG_FILE}" "$1" "$2"
}

# Helper (m7 / #3): install pinned arduino-cli v1.6.0 with verified SHA-256 + timeouts & retries
install_pinned_arduino_cli() {
  local version="1.6.0"
  local arch asset expected_sha
  arch="$(uname -m)"
  case "${arch}" in
    aarch64|arm64)
      asset="arduino-cli_${version}_Linux_ARM64.tar.gz"
      expected_sha="2ed4e1436bb3be1265966af8eba95dd25e28f3a9f92d0a58d371ed14dbaf550f"
      ;;
    armv7l|armhf)
      asset="arduino-cli_${version}_Linux_ARMv7.tar.gz"
      expected_sha="4936285be94c9327d8bc44124123789ed9bf6c0288bf826a4c2dd1ba98548667"
      ;;
    x86_64|amd64)
      asset="arduino-cli_${version}_Linux_64bit.tar.gz"
      expected_sha="321ee249f44c78053234fec0f36101092bc6021bf24939e15e0cc2449c5ed250"
      ;;
    *)
      echo "[ERROR] Unsupported architecture for pinned arduino-cli install: ${arch}" >&2
      return 1
      ;;
  esac

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' RETURN
  local url="https://github.com/arduino/arduino-cli/releases/download/v${version}/${asset}"
  echo "==> Downloading arduino-cli v${version} (${asset})..."
  curl -fsSL --connect-timeout 15 --max-time 180 --retry 3 --retry-all-errors --retry-delay 5 \
    -o "${tmp_dir}/${asset}" "${url}"

  local actual_sha
  actual_sha="$(sha256sum "${tmp_dir}/${asset}" | awk '{print $1}')"
  if [ "${actual_sha}" != "${expected_sha}" ]; then
    echo "[ERROR] arduino-cli SHA-256 mismatch! Expected ${expected_sha}, got ${actual_sha}" >&2
    return 1
  fi
  echo "==> arduino-cli SHA-256 verified (${actual_sha:0:12})."
  tar -xzf "${tmp_dir}/${asset}" -C "${tmp_dir}" arduino-cli
  install -m 0755 "${tmp_dir}/arduino-cli" /usr/local/bin/arduino-cli
}

echo "==> Installing scripts, Web Setup Wizard & firmware profiles to ${INSTALL_DIR}..."
mkdir -p \
  "${INSTALL_DIR}/scripts" \
  "${INSTALL_DIR}/webui/docs" \
  "${INSTALL_DIR}/firmware" \
  "${INSTALL_DIR}/firmware-src" \
  "${CONFIG_DIR}" \
  "${STATE_DIR}/flash-once" \
  "${ARDUINO_CACHE}/staging" \
  "${ARDUINO_CACHE}/user"

chmod 0755 "${STATE_DIR}"
chmod 0700 "${STATE_DIR}/flash-once"

install -m 0644 "${REPO_DIR}/scripts/onstep_common.py" "${INSTALL_DIR}/scripts/onstep_common.py"
install -m 0755 "${REPO_DIR}/scripts/onstep_provision.py" "${INSTALL_DIR}/scripts/onstep_provision.py"
install -m 0755 "${REPO_DIR}/scripts/probe_onstep.py" "${INSTALL_DIR}/scripts/probe_onstep.py"
install -m 0755 "${REPO_DIR}/scripts/update_firmware.sh" "${INSTALL_DIR}/scripts/update_firmware.sh"
install -m 0755 "${REPO_DIR}/scripts/update_software.sh" "${INSTALL_DIR}/scripts/update_software.sh"
install -m 0755 "${REPO_DIR}/scripts/setup_hotspot.sh" "${INSTALL_DIR}/scripts/setup_hotspot.sh"
install -m 0755 "${REPO_DIR}/scripts/configure_nginx_overlay.sh" "${INSTALL_DIR}/scripts/configure_nginx_overlay.sh"
install -m 0755 "${REPO_DIR}/scripts/configure_indi_defaults.sh" "${INSTALL_DIR}/scripts/configure_indi_defaults.sh"
install -m 0755 "${REPO_DIR}/webui/onstep_webui.py" "${INSTALL_DIR}/webui/onstep_webui.py"
install -m 0644 "${REPO_DIR}/webui/index.html" "${INSTALL_DIR}/webui/index.html"
install -m 0644 "${REPO_DIR}/webui/overlay.js" "${INSTALL_DIR}/webui/overlay.js"
install -m 0644 "${REPO_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
if [ -f "${REPO_DIR}/VERSION" ]; then
  install -m 0644 "${REPO_DIR}/VERSION" "${INSTALL_DIR}/VERSION"
fi
if [ -d "${REPO_DIR}/docs" ]; then
  cp -f "${REPO_DIR}/docs/"*.svg "${INSTALL_DIR}/webui/docs/" 2>/dev/null || true
fi

# Copy firmware overlay profiles, pinned ref, and build.sh
install -m 0755 "${REPO_DIR}/firmware/build.sh" "${INSTALL_DIR}/firmware-src/build.sh"
if [ -f "${REPO_DIR}/firmware/onstepx.ref" ]; then
  install -m 0644 "${REPO_DIR}/firmware/onstepx.ref" "${INSTALL_DIR}/firmware-src/onstepx.ref"
fi
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

# m2 + #7: Validate venv health and skip pip install when requirements.txt + Python version are unchanged
echo "==> Setting up Python virtual environment (${INSTALL_DIR}/.venv)..."
VENV_DIR="${INSTALL_DIR}/.venv"
VENV_STAMP_FILE="${VENV_DIR}/.deps-stamp"
if [ -d "${VENV_DIR}" ]; then
  if ! "${VENV_DIR}/bin/python" -c "import sys, pip" >/dev/null 2>&1; then
    echo "==> Existing Python venv is stale or broken; recreating..."
    rm -rf "${VENV_DIR}"
  fi
fi
if [ ! -d "${VENV_DIR}" ]; then
  python3 -m venv "${VENV_DIR}"
fi
DESIRED_STAMP="$(printf '%s\n' "$("${VENV_DIR}/bin/python" --version 2>&1)" "$(sha256sum "${INSTALL_DIR}/requirements.txt" | awk '{print $1}')" | sha256sum | awk '{print $1}')"
CURRENT_STAMP="$(cat "${VENV_STAMP_FILE}" 2>/dev/null || true)"
if [ "${DESIRED_STAMP}" = "${CURRENT_STAMP}" ]; then
  echo "==> Python dependencies already up to date (skipping pip install)."
else
  "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
  "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
  printf '%s\n' "${DESIRED_STAMP}" > "${VENV_STAMP_FILE}"
fi

if [ "${WITH_ARDUINO_CLI}" = "true" ]; then
  echo "==> Installing pinned arduino-cli and ESP8266/ESP32 cores for on-Pi compilation..."
  if ! command -v arduino-cli >/dev/null 2>&1; then
    install_pinned_arduino_cli
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

EXISTING_CONFIG="false"
PRE_META_UPGRADE="false"
if [ ! -f "${CONFIG_FILE}" ]; then
  echo "==> Creating default configuration at ${CONFIG_FILE}..."
  install -m 0644 "${REPO_DIR}/config/onstep-autoprovision.env.example" "${CONFIG_FILE}"
else
  EXISTING_CONFIG="true"
  if [ ! -f "${META_FILE}" ]; then
    PRE_META_UPGRADE="true"
  fi
  echo "==> Preserving existing configuration at ${CONFIG_FILE}."
fi

if [ "${ENABLE_AUTO_FLASH}" = "true" ]; then
  set_config_key "AUTO_FLASH" "true"
elif [ "${EXISTING_CONFIG}" = "true" ]; then
  # M3 migration fix: older setup.sh installs silently armed AUTO_FLASH="true" with empty allowlists.
  # Disarm unattended AUTO_FLASH during upgrade from pre-meta installs (or warn loudly if still armed with empty allowlists).
  CUR_AUTO_FLASH="$(grep -E '^\s*AUTO_FLASH\s*=' "${CONFIG_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '\"'\'' ' || true)"
  CUR_SERIALS="$(grep -E '^\s*ALLOWED_USB_SERIALS\s*=' "${CONFIG_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '\"'\'' ' || true)"
  CUR_PATHS="$(grep -E '^\s*ALLOWED_USB_PATHS\s*=' "${CONFIG_FILE}" | tail -n 1 | cut -d= -f2- | tr -d '\"'\'' ' || true)"
  if [ "${CUR_AUTO_FLASH}" = "true" ]; then
    if [ "${PRE_META_UPGRADE}" = "true" ] || { [ -z "${CUR_SERIALS}" ] && [ -z "${CUR_PATHS}" ]; }; then
      echo "==> [SAFETY MIGRATION] Disabling unattended AUTO_FLASH=\"true\" in preserved ${CONFIG_FILE} (no USB allowlist set)."
      echo "    Use Setup Wizard Step 3 for safe 1-click flashing, or pass --enable-auto-flash explicitly."
      set_config_key "AUTO_FLASH" "false"
    fi
  fi
fi

if [ "${WITH_ARDUINO_CLI}" = "true" ]; then
  set_config_key "AUTO_COMPILE" "true"
fi
if [ -n "${GITHUB_REPO_ARG}" ]; then
  set_config_key "GITHUB_REPO" "${GITHUB_REPO_ARG}"
fi

# M1: Generate persistent Web UI authentication token if not already present
if [ ! -f "${TOKEN_FILE}" ]; then
  echo "==> Generating Web Setup Wizard authentication token (${TOKEN_FILE})..."
  python3 -c 'import secrets; print(secrets.token_hex(16))' > "${TOKEN_FILE}"
  chown root:dialout "${TOKEN_FILE}" 2>/dev/null || chown root:root "${TOKEN_FILE}"
  chmod 0640 "${TOKEN_FILE}"
fi

# Persist install metadata & flags so daily self-updates reproduce the exact install options
INSTALLED_VERSION="$(tr -d '[:space:]' < "${REPO_DIR}/VERSION" 2>/dev/null || echo "1.2.0")"
INSTALLED_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || echo "")"
cat > "${META_FILE}" <<EOF
INSTALLED_VERSION="${INSTALLED_VERSION}"
INSTALLED_COMMIT="${INSTALLED_COMMIT}"
ASTRODRIVE_SRC_DIR="${REPO_DIR}"
INSTALL_WITH_ARDUINO_CLI="${WITH_ARDUINO_CLI}"
INSTALL_ENABLE_AUTO_FLASH="${ENABLE_AUTO_FLASH}"
EOF
chmod 0644 "${META_FILE}"

echo "==> Installing udev rule and systemd units..."
install -m 0644 "${REPO_DIR}/udev/99-onstep-nodemcu.rules" /etc/udev/rules.d/99-onstep-nodemcu.rules
install -m 0644 "${REPO_DIR}/systemd/onstep-autoprovision@.service" /etc/systemd/system/onstep-autoprovision@.service
install -m 0644 "${REPO_DIR}/systemd/onstep-firmware-update.service" /etc/systemd/system/onstep-firmware-update.service
install -m 0644 "${REPO_DIR}/systemd/onstep-firmware-update.timer" /etc/systemd/system/onstep-firmware-update.timer
install -m 0644 "${REPO_DIR}/systemd/astrodrive-update.service" /etc/systemd/system/astrodrive-update.service
install -m 0644 "${REPO_DIR}/systemd/astrodrive-update.timer" /etc/systemd/system/astrodrive-update.timer
install -m 0644 "${REPO_DIR}/systemd/onstep-webui.service" /etc/systemd/system/onstep-webui.service

# Enable background firmware & software update timers + Web UI service
systemctl enable onstep-firmware-update.timer >/dev/null 2>&1 || true
systemctl enable astrodrive-update.timer >/dev/null 2>&1 || true
systemctl enable onstep-webui.service >/dev/null 2>&1 || true

# S2: Install Nginx Astroberry UI overlay and surface any failure loudly (never swallow silently)
OVERLAY_RESULT="not_installed"
if [ -d /etc/nginx ]; then
  mkdir -p /etc/nginx/snippets
  install -m 0644 "${REPO_DIR}/nginx/onstep-astroberry-overlay.conf" /etc/nginx/snippets/onstep-astroberry-overlay.conf
  if "${INSTALL_DIR}/scripts/configure_nginx_overlay.sh"; then
    OVERLAY_RESULT="attached"
  else
    OVERLAY_RESULT="failed"
    echo "[WARNING] Astroberry Nginx overlay could not be attached automatically. Use http://$(hostname).local:8090/ directly." >&2
  fi
  if [ -d /etc/apt/apt.conf.d ]; then
    cat > /etc/apt/apt.conf.d/99-onstep-nginx-overlay <<'EOF'
DPkg::Post-Invoke { "if [ -x /opt/onstep-autoprovision/scripts/configure_nginx_overlay.sh ]; then /opt/onstep-autoprovision/scripts/configure_nginx_overlay.sh || logger -t astrodrive-overlay 'WARNING: Nginx overlay re-attach failed after apt upgrade'; fi"; };
EOF
  fi
fi

# Disable gpsd USBAUTO so it cannot hijack /dev/ttyUSB* from OnStep/INDI
if [ -f /etc/default/gpsd ] && grep -Eq '^\s*USBAUTO\s*=\s*"?true"?' /etc/default/gpsd; then
  echo "==> Disabling USBAUTO in /etc/default/gpsd to prevent serial-port hijacking..."
  sed -i 's/^\s*USBAUTO=.*/USBAUTO="false"/' /etc/default/gpsd
fi

# S2: Pre-configure INDI LX200 OnStep defaults and track result visibly
INDI_RESULT="ok"
if ! "${INSTALL_DIR}/scripts/configure_indi_defaults.sh"; then
  INDI_RESULT="failed"
  echo "[WARNING] Could not pre-configure INDI defaults automatically. You can apply them in Setup Wizard Step 1." >&2
fi

NOW_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
python3 - "${COMMON_PY}" "${STATE_DIR}/install-status.json" "${OVERLAY_RESULT}" "${INDI_RESULT}" "${INSTALLED_VERSION}" "${INSTALLED_COMMIT}" "${NOW_UTC}" <<'PY' 2>/dev/null || true
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("onstep_common", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.write_json_atomic(pathlib.Path(sys.argv[2]), {
    "nginx_overlay": sys.argv[3],
    "indi_defaults": sys.argv[4],
    "installed_version": sys.argv[5],
    "installed_commit": sys.argv[6],
    "installed_at_utc": sys.argv[7],
})
PY

if [ "${CHROOT_MODE}" = "false" ]; then
  systemctl daemon-reload
  systemctl start onstep-firmware-update.timer || true
  systemctl start astrodrive-update.timer || true
  systemctl restart onstep-webui.service || true
  udevadm control --reload-rules
  if [ "${SKIP_UDEV_TRIGGER}" != "true" ]; then
    udevadm trigger --subsystem-match=tty
  fi

  if [ -n "${SUDO_USER:-}" ] && id "${SUDO_USER}" >/dev/null 2>&1; then
    usermod -aG dialout "${SUDO_USER}"
    echo "==> Added ${SUDO_USER} to 'dialout' group (log out/in to apply)."
  fi
else
  echo "==> Chroot mode detected: skipped live daemon reloads (will activate on first boot)."
  for u in astroberry pi; do
    if id "${u}" >/dev/null 2>&1; then
      usermod -aG dialout "${u}" || true
    fi
  done
fi

WEBUI_TOKEN="$(tr -d '[:space:]' < "${TOKEN_FILE}" 2>/dev/null || echo "")"
echo ""
echo "==> AstroDrive v${INSTALLED_VERSION} installed!"
if [ "${OVERLAY_RESULT}" = "attached" ]; then
  echo "  Setup Wizard: http://astroberry.local/onstep/#token=${WEBUI_TOKEN}"
fi
echo "  Direct URL:   http://$(hostname).local:8090/#token=${WEBUI_TOKEN}"
echo "  Setup PIN:    ${WEBUI_TOKEN}  (stored in ${TOKEN_FILE})"
