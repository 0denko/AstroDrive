#!/usr/bin/env bash
# Installer for AstroDrive on Raspberry Pi OS (Bookworm/Trixie)
# and Astroberry OS v3.x (64-bit arm64).
#
# Flags:
#   --without-camera     Skip installing USB camera & EAA live-stacking deps
#                        (v4l-utils, python3-opencv, python3-numpy) and disable camera service
#   --with-camera        Explicitly install & enable USB camera service (now enabled by default)
#   --with-arduino-cli   Install pinned arduino-cli (v1.6.0, SHA-256 verified) +
#                        ESP8266 (3.1.2) & ESP32 (2.0.17) cores for on-Pi compilation
#   --enable-auto-flash  Set AUTO_FLASH="true" in /etc/onstep-autoprovision/config.env
#                        (default is "false" for safety; Wizard offers 1-click flash)
#   --github-repo O/R    Pre-configure GITHUB_REPO="owner/repo" for online updates
#   --skip-udev-trigger  Skip `udevadm trigger` (used during background self-updates)
#   --lock-held          Caller already holds /run/lock/onstep-provision.lock
#   --chroot             Chroot-safe mode (used by .github/workflows/build-pi-image.yml)
set -euo pipefail

ROOT_PREFIX="${ASTRODRIVE_ROOT_PREFIX:-}"
if [ "$(id -u)" -ne 0 ] && [ -z "${ROOT_PREFIX}" ]; then
  echo "[ERROR] Please run with sudo: sudo ./install.sh" >&2
  exit 1
fi

WITH_CAMERA="true"
EXPLICIT_WITH_CAMERA="false"
WITH_ARDUINO_CLI="false"
ENABLE_AUTO_FLASH="false"
SKIP_UDEV_TRIGGER="false"
LOCK_HELD="false"
CHROOT_MODE="false"
GITHUB_REPO_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-camera) WITH_CAMERA="true"; EXPLICIT_WITH_CAMERA="true"; shift ;;
    --without-camera|--no-camera) WITH_CAMERA="false"; shift ;;
    --with-arduino-cli) WITH_ARDUINO_CLI="true"; shift ;;
    --enable-auto-flash) ENABLE_AUTO_FLASH="true"; shift ;;
    --skip-udev-trigger) SKIP_UDEV_TRIGGER="true"; shift ;;
    --lock-held) LOCK_HELD="true"; shift ;;
    --github-repo) GITHUB_REPO_ARG="${2:?Missing owner/repo}"; shift 2 ;;
    --chroot) CHROOT_MODE="true"; shift ;;
    -h|--help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# Auto-detect chroot even if --chroot wasn't passed explicitly
if [ -n "${ROOT_PREFIX}" ] || { command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --chroot >/dev/null 2>&1; }; then
  CHROOT_MODE="true"
fi

# Serialize manual setup.sh / install.sh runs with flashing and background self-updates
if [ "${CHROOT_MODE}" = "false" ] && [ "${LOCK_HELD}" != "true" ] && command -v flock >/dev/null 2>&1; then
  mkdir -p /run/lock
  exec 9>/run/lock/onstep-provision.lock
  if ! flock -w 120 9; then
    echo "[ERROR] Timed out waiting for /run/lock/onstep-provision.lock (another install or flash is running)." >&2
    exit 1
  fi
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${ROOT_PREFIX}/opt/onstep-autoprovision"
CONFIG_DIR="${ROOT_PREFIX}/etc/onstep-autoprovision"
CONFIG_FILE="${CONFIG_DIR}/config.env"
META_FILE="${CONFIG_DIR}/install-meta.env"
TOKEN_FILE="${CONFIG_DIR}/webui.token"
STATE_DIR="${ROOT_PREFIX}/var/lib/onstep-autoprovision"
ARDUINO_CACHE="${ROOT_PREFIX}/var/cache/onstep-arduino"
UDEV_DIR="${ROOT_PREFIX}/etc/udev/rules.d"
SYSTEMD_DIR="${ROOT_PREFIX}/etc/systemd/system"
COMMON_PY="${INSTALL_DIR}/scripts/onstep_common.py"

# Helpers (R1): read & write config.env safely via onstep_common.py (never `source` as root)
set_config_key() {
  python3 "${COMMON_PY}" set-config "${CONFIG_FILE}" "$1" "$2"
}
get_config_key() {
  python3 "${COMMON_PY}" get-config "${CONFIG_FILE}" "$1" "${2:-}" 2>/dev/null || printf '%s' "${2:-}"
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
  "${STATE_DIR}/camera-cmd" \
  "${STATE_DIR}/captures" \
  "${ARDUINO_CACHE}/staging" \
  "${ARDUINO_CACHE}/user"

chmod 0755 "${STATE_DIR}" "${STATE_DIR}/camera-cmd" "${STATE_DIR}/captures"
chmod 0700 "${STATE_DIR}/flash-once"

install -m 0644 "${REPO_DIR}/scripts/onstep_common.py" "${INSTALL_DIR}/scripts/onstep_common.py"
install -m 0755 "${REPO_DIR}/scripts/onstep_provision.py" "${INSTALL_DIR}/scripts/onstep_provision.py"
install -m 0755 "${REPO_DIR}/scripts/probe_onstep.py" "${INSTALL_DIR}/scripts/probe_onstep.py"
install -m 0755 "${REPO_DIR}/scripts/onstep_camera.py" "${INSTALL_DIR}/scripts/onstep_camera.py"
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

if [ "${WITH_CAMERA}" = "true" ] && [ -z "${ROOT_PREFIX}" ]; then
  CAM_PKGS=(v4l-utils python3-opencv python3-numpy)
  MISSING_CAM_PKGS=()
  for pkg in "${CAM_PKGS[@]}"; do
    if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
      MISSING_CAM_PKGS+=("${pkg}")
    fi
  done
  if [ "${#MISSING_CAM_PKGS[@]}" -eq 0 ]; then
    echo "==> Camera packages (${CAM_PKGS[*]}) already installed (skipping apt)."
  else
    echo "==> Installing USB camera & EAA live-stacking packages: ${MISSING_CAM_PKGS[*]}..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${MISSING_CAM_PKGS[@]}"
  fi
fi

# Build new/broken venv in .venv.new and swap atomically so a failed offline pip install never destroys a working venv
echo "==> Setting up Python virtual environment (${INSTALL_DIR}/.venv)..."
VENV_DIR="${INSTALL_DIR}/.venv"
VENV_NEW="${INSTALL_DIR}/.venv.new"
VENV_STAMP_FILE="${VENV_DIR}/.deps-stamp"
NEED_NEW_VENV="false"
if [ ! -d "${VENV_DIR}" ]; then
  NEED_NEW_VENV="true"
elif ! "${VENV_DIR}/bin/python" -c "import sys, pip" >/dev/null 2>&1; then
  echo "==> Existing Python venv is stale or broken; building replacement in ${VENV_NEW}..."
  NEED_NEW_VENV="true"
fi

if [ "${NEED_NEW_VENV}" = "true" ]; then
  rm -rf "${VENV_NEW}"
  if [ "${WITH_CAMERA}" = "true" ]; then
    python3 -m venv --system-site-packages "${VENV_NEW}"
  else
    python3 -m venv "${VENV_NEW}"
  fi
  if [ "${ASTRODRIVE_SKIP_PIP:-false}" != "true" ]; then
    "${VENV_NEW}/bin/pip" install --upgrade pip >/dev/null
    "${VENV_NEW}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
  fi
  NEW_STAMP="$(printf '%s\n' "$("${VENV_NEW}/bin/python" --version 2>&1)" "$(sha256sum "${INSTALL_DIR}/requirements.txt" | awk '{print $1}')" | sha256sum | awk '{print $1}')"
  printf '%s\n' "${NEW_STAMP}" > "${VENV_NEW}/.deps-stamp"
  rm -rf "${VENV_DIR}.old"
  if [ -d "${VENV_DIR}" ]; then
    mv "${VENV_DIR}" "${VENV_DIR}.old"
  fi
  mv "${VENV_NEW}" "${VENV_DIR}"
  rm -rf "${VENV_DIR}.old"
else
  if [ "${WITH_CAMERA}" = "true" ] && [ -f "${VENV_DIR}/pyvenv.cfg" ]; then
    # Expose Debian python3-opencv & python3-numpy inside existing venv without rebuilding wheels on Pi 3B
    sed -i 's/^include-system-site-packages *= *false/include-system-site-packages = true/' "${VENV_DIR}/pyvenv.cfg"
  fi
  DESIRED_STAMP="$(printf '%s\n' "$("${VENV_DIR}/bin/python" --version 2>&1)" "$(sha256sum "${INSTALL_DIR}/requirements.txt" | awk '{print $1}')" | sha256sum | awk '{print $1}')"
  CURRENT_STAMP="$(cat "${VENV_STAMP_FILE}" 2>/dev/null || true)"
  if [ "${DESIRED_STAMP}" = "${CURRENT_STAMP}" ]; then
    echo "==> Python dependencies already up to date (skipping pip install)."
  elif [ "${ASTRODRIVE_SKIP_PIP:-false}" != "true" ]; then
    "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
    "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
    printf '%s\n' "${DESIRED_STAMP}" > "${VENV_STAMP_FILE}"
  fi
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
  CUR_AUTO_FLASH="$(get_config_key AUTO_FLASH false)"
  CUR_SERIALS="$(get_config_key ALLOWED_USB_SERIALS "")"
  CUR_PATHS="$(get_config_key ALLOWED_USB_PATHS "")"
  if [ "${CUR_AUTO_FLASH}" = "true" ]; then
    if [ "${PRE_META_UPGRADE}" = "true" ] || { [ -z "${CUR_SERIALS}" ] && [ -z "${CUR_PATHS}" ]; }; then
      echo "==> [SAFETY MIGRATION] Disabling unattended AUTO_FLASH=\"true\" in preserved ${CONFIG_FILE} (no USB allowlist set)."
      echo "    Use Setup Wizard Step 3 for safe 1-click flashing, or pass --enable-auto-flash explicitly."
      set_config_key "AUTO_FLASH" "false"
    fi
  fi
fi

if [ "${WITH_CAMERA}" = "false" ]; then
  set_config_key "CAMERA_ENABLED" "false"
elif [ "${EXPLICIT_WITH_CAMERA}" = "true" ] || ! grep -Eq '^\s*CAMERA_ENABLED\s*=' "${CONFIG_FILE}"; then
  set_config_key "CAMERA_ENABLED" "true"
fi
if [ "${WITH_ARDUINO_CLI}" = "true" ]; then
  set_config_key "AUTO_COMPILE" "true"
fi
if [ -n "${GITHUB_REPO_ARG}" ]; then
  set_config_key "GITHUB_REPO" "${GITHUB_REPO_ARG}"
fi

# M1: Generate persistent Web UI authentication token if missing, and ALWAYS enforce 0640 permissions
if [ ! -f "${TOKEN_FILE}" ]; then
  echo "==> Generating Web Setup Wizard authentication token (${TOKEN_FILE})..."
  python3 -c 'import secrets; print(secrets.token_hex(16))' > "${TOKEN_FILE}"
fi
if [ -z "${ROOT_PREFIX}" ]; then
  chown root:dialout "${TOKEN_FILE}" 2>/dev/null || chown root:root "${TOKEN_FILE}"
fi
chmod 0640 "${TOKEN_FILE}"

# Persist install metadata & flags so daily self-updates reproduce the exact install options
INSTALLED_VERSION="$(tr -d '[:space:]' < "${REPO_DIR}/VERSION" 2>/dev/null || echo "1.3.0")"
INSTALLED_COMMIT="$(git -C "${REPO_DIR}" rev-parse HEAD 2>/dev/null || echo "")"
cat > "${META_FILE}" <<EOF
INSTALLED_VERSION="${INSTALLED_VERSION}"
INSTALLED_COMMIT="${INSTALLED_COMMIT}"
ASTRODRIVE_SRC_DIR="${REPO_DIR}"
INSTALL_WITH_ARDUINO_CLI="${WITH_ARDUINO_CLI}"
INSTALL_ENABLE_AUTO_FLASH="${ENABLE_AUTO_FLASH}"
INSTALL_WITH_CAMERA="${WITH_CAMERA}"
EOF
chmod 0644 "${META_FILE}"

echo "==> Installing udev rules and systemd units..."
mkdir -p "${UDEV_DIR}" "${SYSTEMD_DIR}"
install -m 0644 "${REPO_DIR}/udev/99-onstep-nodemcu.rules" "${UDEV_DIR}/99-onstep-nodemcu.rules"
install -m 0644 "${REPO_DIR}/udev/99-onstep-camera.rules" "${UDEV_DIR}/99-onstep-camera.rules"
install -m 0644 "${REPO_DIR}/systemd/onstep-autoprovision@.service" "${SYSTEMD_DIR}/onstep-autoprovision@.service"
install -m 0644 "${REPO_DIR}/systemd/onstep-firmware-update.service" "${SYSTEMD_DIR}/onstep-firmware-update.service"
install -m 0644 "${REPO_DIR}/systemd/onstep-firmware-update.timer" "${SYSTEMD_DIR}/onstep-firmware-update.timer"
install -m 0644 "${REPO_DIR}/systemd/astrodrive-update.service" "${SYSTEMD_DIR}/astrodrive-update.service"
install -m 0644 "${REPO_DIR}/systemd/astrodrive-update.timer" "${SYSTEMD_DIR}/astrodrive-update.timer"
install -m 0644 "${REPO_DIR}/systemd/onstep-webui.service" "${SYSTEMD_DIR}/onstep-webui.service"
install -m 0644 "${REPO_DIR}/systemd/onstep-camera.service" "${SYSTEMD_DIR}/onstep-camera.service"

# Enable background firmware & software update timers + Web UI service + camera service (default on)
CUR_CAMERA_ENABLED="$(get_config_key CAMERA_ENABLED true)"
if [ -z "${ROOT_PREFIX}" ]; then
  systemctl enable onstep-firmware-update.timer >/dev/null 2>&1 || true
  systemctl enable astrodrive-update.timer >/dev/null 2>&1 || true
  systemctl enable onstep-webui.service >/dev/null 2>&1 || true
  if [ "${WITH_CAMERA}" = "true" ] && [ "${CUR_CAMERA_ENABLED}" = "true" ]; then
    systemctl enable onstep-camera.service >/dev/null 2>&1 || true
  else
    systemctl disable onstep-camera.service >/dev/null 2>&1 || true
  fi
fi

# S2: Install Nginx Astroberry UI overlay and surface any failure loudly (never swallow silently)
OVERLAY_RESULT="not_installed"
if [ -z "${ROOT_PREFIX}" ] && [ -d /etc/nginx ]; then
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
if [ -z "${ROOT_PREFIX}" ] && [ -f /etc/default/gpsd ] && grep -Eq '^\s*USBAUTO\s*=\s*"?true"?' /etc/default/gpsd; then
  echo "==> Disabling USBAUTO in /etc/default/gpsd to prevent serial-port hijacking..."
  sed -i 's/^\s*USBAUTO=.*/USBAUTO="false"/' /etc/default/gpsd
fi

# S2: Pre-configure INDI LX200 OnStep defaults and track result visibly
INDI_RESULT="ok"
if [ -z "${ROOT_PREFIX}" ]; then
  if ! "${INSTALL_DIR}/scripts/configure_indi_defaults.sh"; then
    INDI_RESULT="failed"
    echo "[WARNING] Could not pre-configure INDI defaults automatically. You can apply them in Setup Wizard Step 1." >&2
  fi
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
  if [ "${WITH_CAMERA}" = "true" ] && [ "${CUR_CAMERA_ENABLED}" = "true" ]; then
    systemctl restart onstep-camera.service || true
  else
    systemctl stop onstep-camera.service >/dev/null 2>&1 || true
  fi
  udevadm control --reload-rules
  if [ "${SKIP_UDEV_TRIGGER}" != "true" ]; then
    udevadm trigger --subsystem-match=tty
    udevadm trigger --subsystem-match=video4linux || true
  fi

  if [ -n "${SUDO_USER:-}" ] && id "${SUDO_USER}" >/dev/null 2>&1; then
    usermod -aG dialout,video "${SUDO_USER}" 2>/dev/null || usermod -aG dialout "${SUDO_USER}"
    echo "==> Added ${SUDO_USER} to 'dialout' and 'video' groups (log out/in to apply)."
  fi
else
  echo "==> Chroot mode detected: skipped live daemon reloads (will activate on first boot)."
  for u in astroberry pi; do
    if id "${u}" >/dev/null 2>&1; then
      usermod -aG dialout,video "${u}" 2>/dev/null || usermod -aG dialout "${u}" || true
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
