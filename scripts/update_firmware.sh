#!/usr/bin/env bash
# Fetches the latest verified OnStepX firmware (.bin + manifest.json) from GitHub Releases.
#
# Usage:
#   ./scripts/update_firmware.sh --repo owner/astro-pi-onstep-autoprovision --profile nodemcu-esp8266-tmc2209s
#   ./scripts/update_firmware.sh --all   # uses GITHUB_REPO from /etc/onstep-autoprovision/config.env
set -euo pipefail

if [ -f /etc/onstep-autoprovision/config.env ]; then
  # shellcheck disable=SC1091
  source /etc/onstep-autoprovision/config.env
fi

GITHUB_REPO="${GITHUB_REPO:-}"
FIRMWARE_DIR="${FIRMWARE_DIR:-/opt/onstep-autoprovision/firmware}"
PROFILE=""
UPDATE_ALL="false"

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
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ -z "${GITHUB_REPO}" ]; then
  echo "[INFO] GITHUB_REPO is not set in /etc/onstep-autoprovision/config.env; skipping online release check."
  exit 0
fi

# Quick connectivity check (do not stall in the field when offline)
if ! curl -fsSL --connect-timeout 5 --max-time 10 "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" >/tmp/onstep_release.json 2>/dev/null; then
  echo "[INFO] Offline or no GitHub Release found for ${GITHUB_REPO}; keeping existing local cache."
  rm -f /tmp/onstep_release.json
  exit 0
fi

fetch_profile_from_release() {
  local prof="$1"
  local manifest_asset="manifest-${prof}.json"
  local bin_asset="onstepx-${prof}.bin"

  local manifest_url bin_url
  manifest_url="$(python3 -c '
import json, sys
data = json.load(open("/tmp/onstep_release.json"))
target = sys.argv[1]
for a in data.get("assets", []):
    if a.get("name") == target:
        print(a.get("browser_download_url", ""))
        break
' "${manifest_asset}")"

  bin_url="$(python3 -c '
import json, sys
data = json.load(open("/tmp/onstep_release.json"))
target = sys.argv[1]
for a in data.get("assets", []):
    if a.get("name") == target:
        print(a.get("browser_download_url", ""))
        break
' "${bin_asset}")"

  if [ -z "${manifest_url}" ] || [ -z "${bin_url}" ]; then
    echo "[INFO] Release does not contain assets for profile '${prof}' (${bin_asset})."
    return 1
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' RETURN

  echo "==> Downloading ${manifest_asset} and ${bin_asset}..."
  curl -fsSL --connect-timeout 10 --max-time 60 -o "${tmp_dir}/manifest.json" "${manifest_url}"

  local dest_dir="${FIRMWARE_DIR}/${prof}"
  local new_sha cur_sha=""
  new_sha="$(python3 -c 'import json; print(json.load(open("'"${tmp_dir}/manifest.json"'")).get("sha256", "").lower())')"
  if [ -f "${dest_dir}/manifest.json" ]; then
    cur_sha="$(python3 -c 'import json; print(json.load(open("'"${dest_dir}/manifest.json"'")).get("sha256", "").lower())' 2>/dev/null || true)"
  fi

  if [ -n "${cur_sha}" ] && [ "${cur_sha}" = "${new_sha}" ] && [ -f "${dest_dir}/${bin_asset}" ]; then
    echo "==> Profile '${prof}' is already up to date (${new_sha:0:12})."
    return 0
  fi

  curl -fsSL --connect-timeout 10 --max-time 120 -o "${tmp_dir}/${bin_asset}" "${bin_url}"

  local actual_sha
  actual_sha="$(sha256sum "${tmp_dir}/${bin_asset}" | awk '{print $1}')"
  if [ "${actual_sha}" != "${new_sha}" ]; then
    echo "[ERROR] SHA-256 mismatch for ${bin_asset} (expected ${new_sha}, got ${actual_sha}); aborting!" >&2
    return 1
  fi

  mkdir -p "${dest_dir}"
  install -m 0644 "${tmp_dir}/${bin_asset}" "${dest_dir}/${bin_asset}"
  install -m 0644 "${tmp_dir}/manifest.json" "${dest_dir}/manifest.json"
  echo "==> Updated ${dest_dir}/${bin_asset} (SHA-256 verified: ${actual_sha:0:12})."
}

if [ "${UPDATE_ALL}" = "true" ]; then
  for p in nodemcu-esp8266-tmc2209s nodemcu-esp32-tmc2209s; do
    fetch_profile_from_release "${p}" || true
  done
else
  fetch_profile_from_release "${PROFILE:-nodemcu-esp8266-tmc2209s}"
fi

rm -f /tmp/onstep_release.json
