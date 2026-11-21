#!/usr/bin/env bash
# Lightweight, safety-gated self-updater for AstroDrive software (scripts, Web UI,
# systemd units, udev rules, firmware profiles) with:
#   - R1: Zero `source` of config.env / install-meta.env as root (parsed via onstep_common.py)
#   - R2: Stage-exec into /run/astrodrive-updater so install.sh never overwrites the running script
#   - R3: Pre-update tarball snapshot of the installed tree + local source backup for offline rollback
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# R2: Copy ourselves + onstep_common.py into tmpfs (/run/astrodrive-updater) and exec from there
# so `install.sh` overwriting `/opt/onstep-autoprovision/scripts/update_software.sh` can never
# corrupt the live bash interpreter reading by byte offset.
if [ "${ASTRODRIVE_STAGED:-0}" != "1" ]; then
  STAGE_DIR="/run/astrodrive-updater"
  mkdir -p "${STAGE_DIR}"
  chmod 0700 "${STAGE_DIR}"
  cp -f "${BASH_SOURCE[0]}" "${STAGE_DIR}/update_software.sh"
  if [ -f "${SCRIPT_DIR}/onstep_common.py" ]; then
    cp -f "${SCRIPT_DIR}/onstep_common.py" "${STAGE_DIR}/onstep_common.py"
  fi
  chmod 0700 "${STAGE_DIR}/update_software.sh"
  exec env ASTRODRIVE_STAGED=1 "${STAGE_DIR}/update_software.sh" "$@"
fi

FORCE="false"
CHECK_ONLY="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE="true"; shift ;;
    --check-only) CHECK_ONLY="true"; shift ;;
    *) echo "[ERROR] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

COMMON_PY="${SCRIPT_DIR}/onstep_common.py"
CONFIG_FILE="/etc/onstep-autoprovision/config.env"
META_FILE="/etc/onstep-autoprovision/install-meta.env"
STATE_DIR="/var/lib/onstep-autoprovision"
STATE_FILE="${STATE_DIR}/software-update.json"
SNAPSHOT_FILE="${STATE_DIR}/rollback-snapshot.tar.gz"
LOCK_FILE="/run/lock/onstep-provision.lock"

# R1: Never `source` config.env or install-meta.env in a root shell!
get_cfg() {
  python3 "${COMMON_PY}" get-config "$1" "$2" "${3:-}" 2>/dev/null || printf '%s' "${3:-}"
}

AUTO_UPDATE_SOFTWARE="$(get_cfg "${CONFIG_FILE}" AUTO_UPDATE_SOFTWARE true)"
GITHUB_REPO="$(get_cfg "${CONFIG_FILE}" GITHUB_REPO 0denko/AstroDrive)"
SRC_DIR="$(get_cfg "${META_FILE}" ASTRODRIVE_SRC_DIR /opt/astrodrive-src)"
INSTALLED_COMMIT="$(get_cfg "${META_FILE}" INSTALLED_COMMIT "")"
INSTALLED_VERSION="$(get_cfg "${META_FILE}" INSTALLED_VERSION unknown)"
INSTALL_WITH_ARDUINO_CLI="$(get_cfg "${META_FILE}" INSTALL_WITH_ARDUINO_CLI false)"
INSTALL_ENABLE_AUTO_FLASH="$(get_cfg "${META_FILE}" INSTALL_ENABLE_AUTO_FLASH false)"
INSTALL_WITH_CAMERA="$(get_cfg "${META_FILE}" INSTALL_WITH_CAMERA true)"

write_state() {
  local status="$1"
  local message="$2"
  local remote_sha="${3:-}"
  local now_utc
  now_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  python3 - "${COMMON_PY}" "${STATE_FILE}" "${status}" "${message}" "${INSTALLED_VERSION}" "${INSTALLED_COMMIT}" "${remote_sha}" "${now_utc}" <<'PY' 2>/dev/null || true
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("onstep_common", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.write_json_atomic(pathlib.Path(sys.argv[2]), {
    "status": sys.argv[3],
    "message": sys.argv[4],
    "installed_version": sys.argv[5],
    "installed_commit": sys.argv[6],
    "remote_commit": sys.argv[7],
    "checked_at_utc": sys.argv[8],
})
PY
}

if [ "${FORCE}" != "true" ] && [ "${CHECK_ONLY}" != "true" ] && [ "${AUTO_UPDATE_SOFTWARE}" != "true" ]; then
  echo "[INFO] AUTO_UPDATE_SOFTWARE is disabled; skipping automatic software update."
  write_state "disabled" "Automatic software updates are disabled in settings."
  exit 0
fi

# Quick remote check (fails cleanly when offline)
REPO_URL="https://github.com/${GITHUB_REPO}.git"
REMOTE_LINE="$(git ls-remote --heads "${REPO_URL}" main 2>/dev/null | head -n 1 || true)"
if [ -z "${REMOTE_LINE}" ]; then
  echo "[INFO] Offline or unable to query ${REPO_URL}; skipping software update check."
  write_state "offline" "Offline — skipped software update check."
  exit 0
fi

REMOTE_SHA="$(echo "${REMOTE_LINE}" | awk '{print $1}')"
REMOTE_SHORT="${REMOTE_SHA:0:7}"

if [ -n "${INSTALLED_COMMIT}" ] && [ "${INSTALLED_COMMIT}" = "${REMOTE_SHA}" ]; then
  echo "==> AstroDrive software is up to date (v${INSTALLED_VERSION}, ${REMOTE_SHORT})."
  write_state "up_to_date" "Up to date (v${INSTALLED_VERSION}, ${REMOTE_SHORT})." "${REMOTE_SHA}"
  exit 0
fi

if [ "${CHECK_ONLY}" = "true" ]; then
  echo "==> Update available: ${INSTALLED_COMMIT:0:7} -> ${REMOTE_SHORT}."
  write_state "update_available" "Update available (${INSTALLED_COMMIT:0:7} -> ${REMOTE_SHORT})." "${REMOTE_SHA}"
  exit 0
fi

# Safety gate: defer if INDI is active or if flashing/install holds the provision lock
if python3 "${COMMON_PY}" indi-active; then
  echo "[INFO] Active INDI telescope session detected; deferring software update until next cycle."
  write_state "deferred_busy" "Update available (${REMOTE_SHORT}), deferred while INDI session is active." "${REMOTE_SHA}"
  exit 0
fi

mkdir -p "$(dirname "${LOCK_FILE}")" "${STATE_DIR}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[INFO] Provisioning/flashing/install is in progress; deferring software update until next cycle."
  write_state "deferred_busy" "Update available (${REMOTE_SHORT}), deferred while another operation is active." "${REMOTE_SHA}"
  exit 0
fi

# R3: Snapshot the currently installed tree + keep a local backup of ${SRC_DIR} so rollback
# restores exact files without needing `git fetch origin <arbitrary-SHA>` over the network.
PREV_SHA=""
if [ -d "${SRC_DIR}/.git" ]; then
  PREV_SHA="$(git -C "${SRC_DIR}" rev-parse HEAD 2>/dev/null || true)"
fi

SNAPSHOT_PATHS=()
for p in \
  /opt/onstep-autoprovision/scripts \
  /opt/onstep-autoprovision/webui \
  /opt/onstep-autoprovision/firmware-src \
  /opt/onstep-autoprovision/VERSION \
  /opt/onstep-autoprovision/requirements.txt \
  /etc/onstep-autoprovision/install-meta.env \
  /etc/udev/rules.d/99-onstep-*.rules \
  /etc/systemd/system/onstep-*.service \
  /etc/systemd/system/onstep-*.timer \
  /etc/systemd/system/astrodrive-update.service \
  /etc/systemd/system/astrodrive-update.timer; do
  if [ -e "${p}" ]; then
    SNAPSHOT_PATHS+=("${p#/}")
  fi
done

if [ "${#SNAPSHOT_PATHS[@]}" -gt 0 ]; then
  echo "==> Creating pre-update rollback snapshot (${SNAPSHOT_FILE})..."
  tar -czf "${SNAPSHOT_FILE}.tmp" -C / "${SNAPSHOT_PATHS[@]}" 2>/dev/null && mv -f "${SNAPSHOT_FILE}.tmp" "${SNAPSHOT_FILE}" || rm -f "${SNAPSHOT_FILE}.tmp"
fi

SRC_BACKUP="${SRC_DIR}.prev"
rm -rf "${SRC_BACKUP}"
if [ -d "${SRC_DIR}" ]; then
  cp -a "${SRC_DIR}" "${SRC_BACKUP}" || true
fi

echo "==> Updating AstroDrive software (${INSTALLED_COMMIT:0:7} -> ${REMOTE_SHORT})..."
if [ -d "${SRC_DIR}/.git" ]; then
  echo "==> Fetching latest main into ${SRC_DIR} (resetting to origin/main)..."
  git -C "${SRC_DIR}" remote set-url origin "${REPO_URL}"
  if ! (git -C "${SRC_DIR}" fetch --depth 1 origin main && git -C "${SRC_DIR}" reset --hard origin/main); then
    echo "[ERROR] Failed to fetch AstroDrive update from ${REPO_URL}." >&2
    rm -rf "${SRC_BACKUP}"
    write_state "error" "Git fetch failed for ${GITHUB_REPO}." "${REMOTE_SHA}"
    exit 1
  fi
else
  rm -rf "${SRC_DIR}"
  if ! git clone --depth 1 "${REPO_URL}" "${SRC_DIR}"; then
    echo "[ERROR] Failed to clone ${REPO_URL}." >&2
    if [ -d "${SRC_BACKUP}" ]; then
      mv -f "${SRC_BACKUP}" "${SRC_DIR}" || true
    fi
    write_state "error" "Git clone failed for ${GITHUB_REPO}." "${REMOTE_SHA}"
    exit 1
  fi
fi

INSTALL_ARGS=(--github-repo "${GITHUB_REPO}" --skip-udev-trigger --lock-held)
if [ "${INSTALL_WITH_ARDUINO_CLI}" = "true" ]; then
  INSTALL_ARGS+=(--with-arduino-cli)
fi
if [ "${INSTALL_ENABLE_AUTO_FLASH}" = "true" ]; then
  INSTALL_ARGS+=(--enable-auto-flash)
fi
if [ "${INSTALL_WITH_CAMERA}" = "false" ]; then
  INSTALL_ARGS+=(--without-camera)
fi

chmod +x "${SRC_DIR}/install.sh"
if "${SRC_DIR}/install.sh" "${INSTALL_ARGS[@]}"; then
  rm -rf "${SRC_BACKUP}"
  NEW_VER="$(tr -d '[:space:]' < "${SRC_DIR}/VERSION" 2>/dev/null || echo "${INSTALLED_VERSION}")"
  INSTALLED_VERSION="${NEW_VER}"
  INSTALLED_COMMIT="${REMOTE_SHA}"
  echo "==> AstroDrive software updated to v${NEW_VER} (${REMOTE_SHORT})."
  write_state "updated" "Updated to v${NEW_VER} (${REMOTE_SHORT})." "${REMOTE_SHA}"
else
  echo "[ERROR] install.sh failed on ${REMOTE_SHORT}; restoring pre-update snapshot..." >&2
  ROLLBACK_OK="false"
  if [ -f "${SNAPSHOT_FILE}" ]; then
    if tar -xzf "${SNAPSHOT_FILE}" -C /; then
      ROLLBACK_OK="true"
    fi
  fi
  if [ -d "${SRC_BACKUP}" ]; then
    rm -rf "${SRC_DIR}"
    mv -f "${SRC_BACKUP}" "${SRC_DIR}" || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl restart onstep-webui.service >/dev/null 2>&1 || true
  if [ "${INSTALL_WITH_CAMERA}" != "false" ]; then
    systemctl restart onstep-camera.service >/dev/null 2>&1 || true
  fi
  if [ "${ROLLBACK_OK}" = "true" ]; then
    echo "==> Restored previous installed tree (${PREV_SHA:0:7}) from snapshot."
    write_state "error" "Update to ${REMOTE_SHORT} failed and restored previous installed tree (${PREV_SHA:0:7})." "${REMOTE_SHA}"
  else
    write_state "error" "Self-update to ${REMOTE_SHORT} failed (check journalctl -u astrodrive-update)." "${REMOTE_SHA}"
  fi
  exit 1
fi
