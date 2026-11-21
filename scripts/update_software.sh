#!/usr/bin/env bash
# Lightweight, safety-gated self-updater for AstroDrive software (scripts, Web UI,
# systemd units, udev rules, firmware profiles) with automatic rollback on failure (#10).
set -euo pipefail

FORCE="false"
CHECK_ONLY="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE="true"; shift ;;
    --check-only) CHECK_ONLY="true"; shift ;;
    *) echo "[ERROR] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_PY="${SCRIPT_DIR}/onstep_common.py"
CONFIG_FILE="/etc/onstep-autoprovision/config.env"
META_FILE="/etc/onstep-autoprovision/install-meta.env"
STATE_DIR="/var/lib/onstep-autoprovision"
STATE_FILE="${STATE_DIR}/software-update.json"
LOCK_FILE="/run/lock/onstep-provision.lock"

if [ -f "${CONFIG_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${CONFIG_FILE}"
fi
if [ -f "${META_FILE}" ]; then
  # shellcheck disable=SC1090
  source "${META_FILE}"
fi

AUTO_UPDATE_SOFTWARE="${AUTO_UPDATE_SOFTWARE:-true}"
GITHUB_REPO="${GITHUB_REPO:-0denko/AstroDrive}"
SRC_DIR="${ASTRODRIVE_SRC_DIR:-/opt/astrodrive-src}"
INSTALLED_COMMIT="${INSTALLED_COMMIT:-}"
INSTALLED_VERSION="${INSTALLED_VERSION:-unknown}"
INSTALL_WITH_ARDUINO_CLI="${INSTALL_WITH_ARDUINO_CLI:-false}"
INSTALL_ENABLE_AUTO_FLASH="${INSTALL_ENABLE_AUTO_FLASH:-false}"
INSTALL_WITH_CAMERA="${INSTALL_WITH_CAMERA:-false}"

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

# Safety gate (#12): defer if INDI is active (via shared onstep_common.py) or if flashing holds lock
if python3 "${COMMON_PY}" indi-active; then
  echo "[INFO] Active INDI telescope session detected; deferring software update until next cycle."
  write_state "deferred_busy" "Update available (${REMOTE_SHORT}), deferred while INDI session is active." "${REMOTE_SHA}"
  exit 0
fi

mkdir -p "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[INFO] Provisioning/flashing is in progress; deferring software update until next cycle."
  write_state "deferred_busy" "Update available (${REMOTE_SHORT}), deferred while flashing is in progress." "${REMOTE_SHA}"
  exit 0
fi

# #10: Record previous commit before fetching so we can roll back cleanly if install.sh fails
PREV_SHA=""
if [ -d "${SRC_DIR}/.git" ]; then
  PREV_SHA="$(git -C "${SRC_DIR}" rev-parse HEAD 2>/dev/null || true)"
fi

echo "==> Updating AstroDrive software (${INSTALLED_COMMIT:0:7} -> ${REMOTE_SHORT})..."
if [ -d "${SRC_DIR}/.git" ]; then
  echo "==> Fetching latest main into ${SRC_DIR} (resetting to origin/main)..."
  git -C "${SRC_DIR}" remote set-url origin "${REPO_URL}"
  if ! (git -C "${SRC_DIR}" fetch --depth 1 origin main && git -C "${SRC_DIR}" reset --hard origin/main); then
    echo "[ERROR] Failed to fetch AstroDrive update from ${REPO_URL}." >&2
    write_state "error" "Git fetch failed for ${GITHUB_REPO}." "${REMOTE_SHA}"
    exit 1
  fi
else
  rm -rf "${SRC_DIR}"
  if ! git clone --depth 1 "${REPO_URL}" "${SRC_DIR}"; then
    echo "[ERROR] Failed to clone ${REPO_URL}." >&2
    write_state "error" "Git clone failed for ${GITHUB_REPO}." "${REMOTE_SHA}"
    exit 1
  fi
fi

INSTALL_ARGS=(--github-repo "${GITHUB_REPO}" --skip-udev-trigger)
if [ "${INSTALL_WITH_ARDUINO_CLI}" = "true" ]; then
  INSTALL_ARGS+=(--with-arduino-cli)
fi
if [ "${INSTALL_ENABLE_AUTO_FLASH}" = "true" ]; then
  INSTALL_ARGS+=(--enable-auto-flash)
fi
if [ "${INSTALL_WITH_CAMERA}" = "true" ]; then
  INSTALL_ARGS+=(--with-camera)
fi

chmod +x "${SRC_DIR}/install.sh"
if "${SRC_DIR}/install.sh" "${INSTALL_ARGS[@]}"; then
  NEW_VER="$(tr -d '[:space:]' < "${SRC_DIR}/VERSION" 2>/dev/null || echo "${INSTALLED_VERSION}")"
  INSTALLED_VERSION="${NEW_VER}"
  INSTALLED_COMMIT="${REMOTE_SHA}"
  echo "==> AstroDrive software updated to v${NEW_VER} (${REMOTE_SHORT})."
  write_state "updated" "Updated to v${NEW_VER} (${REMOTE_SHORT})." "${REMOTE_SHA}"
else
  echo "[ERROR] install.sh failed on ${REMOTE_SHORT}; attempting rollback..." >&2
  if [ -n "${PREV_SHA}" ] && [ -d "${SRC_DIR}/.git" ]; then
    if git -C "${SRC_DIR}" fetch --depth 1 origin "${PREV_SHA}" >/dev/null 2>&1 || git -C "${SRC_DIR}" cat-file -e "${PREV_SHA}^{commit}" 2>/dev/null; then
      git -C "${SRC_DIR}" reset --hard "${PREV_SHA}" || true
      chmod +x "${SRC_DIR}/install.sh" 2>/dev/null || true
      if "${SRC_DIR}/install.sh" "${INSTALL_ARGS[@]}"; then
        echo "==> Rollback to ${PREV_SHA:0:7} succeeded."
        write_state "error" "Update to ${REMOTE_SHORT} failed and rolled back cleanly to ${PREV_SHA:0:7}." "${REMOTE_SHA}"
        exit 1
      fi
    fi
  fi
  write_state "error" "Self-update to ${REMOTE_SHORT} failed (check journalctl -u astrodrive-update)." "${REMOTE_SHA}"
  exit 1
fi
