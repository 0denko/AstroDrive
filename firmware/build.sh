#!/usr/bin/env bash
# Reproducible multi-profile OnStepX firmware builder using arduino-cli.
#
# Pulls the latest upstream hjd1964/OnStepX, overlays the selected board profile's
# Config.h + Extended.config.h, compiles with pinned Arduino cores, and emits:
#   <out-dir>/<profile>/<OUT_BIN>
#   <out-dir>/<profile>/manifest.json   (chip, flash_offset, version, SHA-256)
#
# Usage:
#   ./firmware/build.sh                                    # Default: nodemcu-esp32-tmc2208s
#   ./firmware/build.sh --profile nodemcu-esp8266-tmc2208s # Specific profile
#   ./firmware/build.sh --all                              # Build every profile
#   ./firmware/build.sh --profile nodemcu-esp32-tmc2208s --out-dir /opt/onstep-autoprovision/firmware
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONSTEPX_REPO="${ONSTEPX_REPO:-https://github.com/hjd1964/OnStepX.git}"
DEFAULT_REF="main"
if [ -f "${SCRIPT_DIR}/onstepx.ref" ]; then
  DEFAULT_REF="$(tr -d '[:space:]' < "${SCRIPT_DIR}/onstepx.ref")"
fi
ONSTEPX_REF="${ONSTEPX_REF:-${DEFAULT_REF}}"
SRC_DIR="${SCRIPT_DIR}/src/OnStepX"
BUILD_ROOT="${SCRIPT_DIR}/.build"
OUT_ROOT="${SCRIPT_DIR}/prebuilt"
ESPTOOL_PY="${ESPTOOL_PY:-}"

PROFILE=""
BUILD_ALL="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:?Missing profile name}"
      shift 2
      ;;
    --all)
      BUILD_ALL="true"
      shift
      ;;
    --out-dir)
      OUT_ROOT="${2:?Missing output directory}"
      shift 2
      ;;
    --ref)
      ONSTEPX_REF="${2:?Missing OnStepX git ref}"
      shift 2
      ;;
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

if ! command -v arduino-cli >/dev/null 2>&1; then
  echo "[ERROR] arduino-cli is not installed. Install from https://arduino.github.io/arduino-cli/" >&2
  exit 1
fi

# Locate esptool for ESP32 merged-binary generation (bootloader + partitions + app -> single .bin)
find_esptool_cmd() {
  if [ -n "${ESPTOOL_PY}" ]; then
    echo "${ESPTOOL_PY}"
    return
  fi
  if [ -x "/opt/onstep-autoprovision/.venv/bin/python" ]; then
    echo "/opt/onstep-autoprovision/.venv/bin/python -m esptool"
    return
  fi
  if command -v esptool.py >/dev/null 2>&1; then
    echo "esptool.py"
    return
  fi
  if command -v esptool >/dev/null 2>&1; then
    echo "esptool"
    return
  fi
  echo "python3 -m esptool"
}

sync_onstepx_source() {
  if [ ! -d "${SRC_DIR}/.git" ]; then
    echo "==> Fetching upstream OnStepX (${ONSTEPX_REF})..."
    mkdir -p "${SRC_DIR}"
    git -C "${SRC_DIR}" init -q
    git -C "${SRC_DIR}" remote add origin "${ONSTEPX_REPO}"
  else
    echo "==> Updating upstream OnStepX (${ONSTEPX_REF})..."
    git -C "${SRC_DIR}" remote set-url origin "${ONSTEPX_REPO}"
  fi
  git -C "${SRC_DIR}" fetch --depth 1 origin "${ONSTEPX_REF}"
  git -C "${SRC_DIR}" checkout -f FETCH_HEAD
  git -C "${SRC_DIR}" clean -fd
}

build_profile() {
  local prof_name="$1"
  local prof_dir="${SCRIPT_DIR}/profiles/${prof_name}"
  if [ ! -f "${prof_dir}/profile.env" ]; then
    echo "[ERROR] Profile not found: ${prof_dir}" >&2
    exit 1
  fi

  # shellcheck disable=SC1090
  source "${prof_dir}/profile.env"

  echo ""
  echo "================================================================="
  echo "==> Building profile: ${PROFILE_NAME} (${TARGET_CHIP}, ${CORE_SPEC})"
  echo "================================================================="

  arduino-cli config init --overwrite >/dev/null 2>&1 || true
  arduino-cli config add board_manager.additional_urls "${CORE_INDEX_URL}" >/dev/null
  arduino-cli core update-index
  arduino-cli core install "${CORE_SPEC}"

  # Reset upstream src/plugins cleanly so profiles never bleed plugins into each other
  git -C "${SRC_DIR}" checkout -- src/plugins/Plugins.config.h >/dev/null 2>&1 || true
  git -C "${SRC_DIR}" clean -fd src/plugins >/dev/null 2>&1 || true

  cp "${prof_dir}/Config.h" "${SRC_DIR}/Config.h"
  cp "${prof_dir}/Extended.config.h" "${SRC_DIR}/Extended.config.h"
  if [ -d "${prof_dir}/plugins" ]; then
    cp -r "${prof_dir}/plugins/"* "${SRC_DIR}/src/plugins/"
  fi
  if [ -f "${prof_dir}/Plugins.config.h" ]; then
    cp "${prof_dir}/Plugins.config.h" "${SRC_DIR}/src/plugins/Plugins.config.h"
  fi

  local build_dir="${BUILD_ROOT}/${PROFILE_NAME}"
  local out_dir="${OUT_ROOT}/${PROFILE_NAME}"
  rm -rf "${build_dir}"
  mkdir -p "${build_dir}/out" "${out_dir}"

  echo "==> Compiling OnStepX for ${FQBN}..."
  arduino-cli compile \
    --fqbn "${FQBN}" \
    --build-path "${build_dir}" \
    --output-dir "${build_dir}/out" \
    "${SRC_DIR}"

  if [ "${TARGET_CHIP}" = "esp32" ]; then
    local app_bin="${build_dir}/out/OnStepX.ino.bin"
    local boot_bin="${build_dir}/out/OnStepX.ino.bootloader.bin"
    local part_bin="${build_dir}/out/OnStepX.ino.partitions.bin"
    local merged_bin="${build_dir}/out/OnStepX.ino.merged.bin"

    if [ -f "${merged_bin}" ]; then
      cp "${merged_bin}" "${out_dir}/${OUT_BIN}"
    elif [ -f "${boot_bin}" ] && [ -f "${part_bin}" ] && [ -f "${app_bin}" ]; then
      echo "==> Merging ESP32 bootloader + partitions + app into single ${OUT_BIN}..."
      local esptool_cmd
      esptool_cmd="$(find_esptool_cmd)"
      # shellcheck disable=SC2086
      ${esptool_cmd} --chip esp32 merge_bin \
        -o "${out_dir}/${OUT_BIN}" \
        --flash_mode "${FLASH_MODE}" \
        --flash_size 4MB \
        0x1000 "${boot_bin}" \
        0x8000 "${part_bin}" \
        0x10000 "${app_bin}"
    else
      echo "[ERROR] Expected ESP32 output binaries not found in ${build_dir}/out" >&2
      ls -la "${build_dir}/out" >&2
      exit 1
    fi
  else
    local compiled_bin="${build_dir}/out/OnStepX.ino.bin"
    if [ ! -f "${compiled_bin}" ]; then
      echo "[ERROR] Expected ESP8266 output binary not found: ${compiled_bin}" >&2
      ls -la "${build_dir}/out" >&2
      exit 1
    fi
    cp "${compiled_bin}" "${out_dir}/${OUT_BIN}"
  fi

  local sha256 ver_major ver_minor ver_patch fw_version git_sha build_utc
  sha256="$(sha256sum "${out_dir}/${OUT_BIN}" | awk '{print $1}')"
  ver_major="$(grep -E '#define[[:space:]]+FirmwareVersionMajor[[:space:]]+' "${SRC_DIR}/OnStepX.ino" | awk '{print $3}' | tr -d '\r' || true)"
  ver_minor="$(grep -E '#define[[:space:]]+FirmwareVersionMinor[[:space:]]+' "${SRC_DIR}/OnStepX.ino" | awk '{print $3}' | tr -d '\r' || true)"
  ver_patch="$(grep -E '#define[[:space:]]+FirmwareVersionPatch[[:space:]]+' "${SRC_DIR}/OnStepX.ino" | awk '{print $3}' | tr -d '\"' | tr -d '\r' || true)"
  if [ -z "${ver_major}" ] || [ -z "${ver_minor}" ]; then
    echo "[ERROR] Failed to parse FirmwareVersionMajor/Minor from ${SRC_DIR}/OnStepX.ino" >&2
    exit 1
  fi
  fw_version="${ver_major}.${ver_minor}${ver_patch}"
  git_sha="$(git -C "${SRC_DIR}" rev-parse --short HEAD)"
  build_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  cat > "${out_dir}/manifest.json" <<EOF
{
  "profile": "${PROFILE_NAME}",
  "binary": "${OUT_BIN}",
  "chip": "${TARGET_CHIP}",
  "fqbn": "${FQBN}",
  "flash_offset": "${FLASH_OFFSET}",
  "flash_mode": "${FLASH_MODE}",
  "firmware_version": "${fw_version}",
  "onstepx_commit": "${git_sha}",
  "arduino_core": "${CORE_SPEC}",
  "sha256": "${sha256}",
  "built_at_utc": "${build_utc}"
}
EOF

  echo "==> Built ${PROFILE_NAME}: OnStepX v${fw_version} (${git_sha})"
  echo "    Binary:   ${out_dir}/${OUT_BIN}"
  echo "    Manifest: ${out_dir}/manifest.json"
  echo "    SHA-256:  ${sha256}"
}

sync_onstepx_source

if [ "${BUILD_ALL}" = "true" ]; then
  for d in "${SCRIPT_DIR}/profiles/"*; do
    [ -f "${d}/profile.env" ] && build_profile "$(basename "${d}")"
  done
else
  build_profile "${PROFILE:-nodemcu-esp32-tmc2208s}"
fi
