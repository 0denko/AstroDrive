#!/usr/bin/env bash
# Fetches the latest OnStepX firmware (.bin + manifest.json) from GitHub Releases,
# verifies exact profile/chip/binary metadata and SHA-256 against the release manifest,
# and installs atomically into the local firmware cache.
#
# Usage:
#   ./scripts/update_firmware.sh --repo 0denko/AstroDrive --profile nodemcu-esp32-tmc2208s
#   ./scripts/update_firmware.sh --all [--allow-offline]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_PY="${SCRIPT_DIR}/onstep_common.py"
CONFIG_FILE="/etc/onstep-autoprovision/config.env"

# R1: Never `source` config.env in a root shell; parse safely via onstep_common.py
get_cfg() {
  if [ -f "${COMMON_PY}" ] && [ -f "${CONFIG_FILE}" ]; then
    python3 "${COMMON_PY}" get-config "${CONFIG_FILE}" "$1" "${2:-}" 2>/dev/null || printf '%s' "${2:-}"
  else
    printf '%s' "${2:-}"
  fi
}

GITHUB_REPO="${GITHUB_REPO:-$(get_cfg GITHUB_REPO "")}"
FIRMWARE_DIR="${FIRMWARE_DIR:-$(get_cfg FIRMWARE_DIR /opt/onstep-autoprovision/firmware)}"
PROFILES_DIR="${PROFILES_DIR:-}"
PROFILE=""
UPDATE_ALL="false"
ALLOW_OFFLINE="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      GITHUB_REPO="${2:?Missing owner/repo}"
      shift 2
      ;;
    --profile)
      PROFILE="${2:?Missing profile name}"
      shift 2
      ;;
    --all)
      UPDATE_ALL="true"
      shift
      ;;
    --out-dir)
      FIRMWARE_DIR="${2:?Missing target directory}"
      shift 2
      ;;
    --allow-offline)
      ALLOW_OFFLINE="true"
      shift
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ -z "${GITHUB_REPO}" ]; then
  echo "[ERROR] GITHUB_REPO is not set in /etc/onstep-autoprovision/config.env." >&2
  exit 1
fi
if ! [[ "${GITHUB_REPO}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
  echo "[ERROR] Invalid GITHUB_REPO format: ${GITHUB_REPO}" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# R13: Resolve the newest `firmware-*` release tag directly via GitHub Releases API
# so non-firmware releases (e.g. Pi images or software tags) can never break firmware updates.
# Falls back to `releases/latest/download` cleanly if the API is rate-limited.
resolve_release_base() {
  local api_json="${WORK_DIR}/releases.json"
  if curl -fsSL --connect-timeout 5 --max-time 15 \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${GITHUB_REPO}/releases?per_page=20" \
      -o "${api_json}" 2>/dev/null; then
    local fw_tag
    fw_tag="$(python3 - "${api_json}" <<'PY' 2>/dev/null || true
import json, sys
try:
    rels = json.load(open(sys.argv[1], encoding="utf-8"))
    for r in rels:
        if r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name", ""))
        if tag.startswith("firmware-"):
            print(tag)
            break
except Exception:
    pass
PY
)"
    if [ -n "${fw_tag}" ]; then
      echo "https://github.com/${GITHUB_REPO}/releases/download/${fw_tag}"
      return 0
    fi
  fi

  # Fallback: releases/latest
  if curl -fsSIL --connect-timeout 5 --max-time 10 "https://github.com/${GITHUB_REPO}/releases/latest" >/dev/null 2>&1; then
    echo "https://github.com/${GITHUB_REPO}/releases/latest/download"
    return 0
  fi
  return 1
}

if ! RELEASE_BASE="$(resolve_release_base)"; then
  if [ "${ALLOW_OFFLINE}" = "true" ]; then
    echo "[INFO] Offline or no GitHub Release reachable for ${GITHUB_REPO}; keeping existing local cache."
    exit 0
  fi
  echo "[ERROR] Unable to reach GitHub Releases for ${GITHUB_REPO} (offline or no release published)." >&2
  exit 2
fi

discover_profiles() {
  local search_dir=""
  if [ -n "${PROFILES_DIR}" ] && [ -d "${PROFILES_DIR}" ]; then
    search_dir="${PROFILES_DIR}"
  elif [ -d "/opt/onstep-autoprovision/firmware-src/profiles" ]; then
    search_dir="/opt/onstep-autoprovision/firmware-src/profiles"
  elif [ -d "${SCRIPT_DIR}/../firmware/profiles" ]; then
    search_dir="${SCRIPT_DIR}/../firmware/profiles"
  fi

  local found=0
  if [ -n "${search_dir}" ]; then
    for d in "${search_dir}"/*; do
      if [ -f "${d}/profile.env" ]; then
        basename "${d}"
        found=1
      fi
    done
  fi
  if [ "${found}" -eq 0 ]; then
    printf '%s\n' "nodemcu-esp32-tmc2208s" "nodemcu-esp8266-tmc2208s"
  fi
}

fetch_profile_from_release() {
  local prof="$1"
  if ! [[ "${prof}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "[ERROR] Invalid profile name: ${prof}" >&2
    return 1
  fi

  local manifest_asset="manifest-${prof}.json"
  local bin_asset="onstepx-${prof}.bin"
  local manifest_url="${RELEASE_BASE}/${manifest_asset}"
  local bin_url="${RELEASE_BASE}/${bin_asset}"

  local tmp_dir="${WORK_DIR}/${prof}"
  mkdir -p "${tmp_dir}"

  echo "==> Checking ${manifest_asset} from ${RELEASE_BASE}..."
  if ! curl -fsSL --connect-timeout 10 --max-time 60 --retry 3 --retry-all-errors --retry-delay 5 \
    -o "${tmp_dir}/manifest.json" "${manifest_url}"; then
    echo "[ERROR] Failed to download ${manifest_url}" >&2
    return 1
  fi

  # R6: Strictly validate profile, chip, binary filename, and sha256 inside manifest.json
  local new_sha
  if ! new_sha="$(python3 - "${tmp_dir}/manifest.json" "${prof}" "${bin_asset}" <<'PY'
import json, sys
mf = json.load(open(sys.argv[1], encoding="utf-8"))
exp_prof, exp_bin = sys.argv[2], sys.argv[3]
if mf.get("profile") != exp_prof:
    raise SystemExit(f"manifest profile mismatch: expected {exp_prof!r}, got {mf.get('profile')!r}")
if mf.get("chip") not in {"esp8266", "esp32", "esp32s3", "esp32c3"}:
    raise SystemExit(f"manifest missing/invalid chip: {mf.get('chip')!r}")
if mf.get("binary") != exp_bin:
    raise SystemExit(f"manifest binary mismatch: expected {exp_bin!r}, got {mf.get('binary')!r}")
sha = str(mf.get("sha256", "")).strip().lower()
if len(sha) != 64:
    raise SystemExit("manifest missing valid 64-hex sha256")
print(sha)
PY
  )"; then
    echo "[ERROR] Invalid manifest metadata for profile '${prof}'." >&2
    return 1
  fi

  local dest_dir="${FIRMWARE_DIR}/${prof}"
  local cur_sha=""
  if [ -f "${dest_dir}/manifest.json" ]; then
    cur_sha="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("sha256", "").lower())' "${dest_dir}/manifest.json" 2>/dev/null || true)"
  fi

  if [ -n "${cur_sha}" ] && [ "${cur_sha}" = "${new_sha}" ] && [ -f "${dest_dir}/${bin_asset}" ]; then
    echo "==> Profile '${prof}' is already up to date (${new_sha:0:12})."
    return 0
  fi

  echo "==> Downloading ${bin_asset}..."
  if ! curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 --retry-all-errors --retry-delay 5 \
    -o "${tmp_dir}/${bin_asset}" "${bin_url}"; then
    echo "[ERROR] Failed to download ${bin_url}" >&2
    return 1
  fi

  local actual_sha
  actual_sha="$(sha256sum "${tmp_dir}/${bin_asset}" | awk '{print $1}')"
  if [ "${actual_sha}" != "${new_sha}" ]; then
    echo "[ERROR] SHA-256 mismatch for ${bin_asset} (expected ${new_sha}, got ${actual_sha}); aborting!" >&2
    return 1
  fi

  mkdir -p "${dest_dir}"
  install -m 0644 "${tmp_dir}/${bin_asset}" "${dest_dir}/${bin_asset}"
  install -m 0644 "${tmp_dir}/manifest.json" "${dest_dir}/manifest.json"
  echo "==> Updated ${dest_dir}/${bin_asset} (manifest SHA-256 matched: ${actual_sha:0:12})."
}

if [ "${UPDATE_ALL}" = "true" ]; then
  FAILED=0
  while IFS= read -r p; do
    [ -z "${p}" ] && continue
    if ! fetch_profile_from_release "${p}"; then
      FAILED=1
    fi
  done < <(discover_profiles)
  if [ "${FAILED}" -ne 0 ]; then
    echo "[ERROR] One or more firmware profiles failed to download or failed verification." >&2
    exit 1
  fi
else
  fetch_profile_from_release "${PROFILE:-nodemcu-esp32-tmc2208s}"
fi
