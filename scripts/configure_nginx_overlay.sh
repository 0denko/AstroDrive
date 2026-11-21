#!/usr/bin/env bash
# Safely attaches /etc/nginx/snippets/onstep-astroberry-overlay.conf to Astroberry OS's
# Nginx server block (idempotent, validated with `nginx -t` with automatic rollback).
#
# Records machine-readable status to /var/lib/onstep-autoprovision/overlay-status.json
# so the Setup Wizard and installer can surface any attachment failure clearly.
set -euo pipefail

SNIPPET_PATH="/etc/nginx/snippets/onstep-astroberry-overlay.conf"
INCLUDE_LINE="    include ${SNIPPET_PATH};"
STATE_DIR="/var/lib/onstep-autoprovision"
STATE_FILE="${STATE_DIR}/overlay-status.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON_PY="${SCRIPT_DIR}/onstep_common.py"

write_status() {
  local status="$1"
  local detail="$2"
  local now_utc
  now_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  mkdir -p "${STATE_DIR}" 2>/dev/null || true
  if [ -f "${COMMON_PY}" ]; then
    python3 - "${COMMON_PY}" "${STATE_FILE}" "${status}" "${detail}" "${now_utc}" <<'PY' 2>/dev/null || true
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("onstep_common", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.write_json_atomic(pathlib.Path(sys.argv[2]), {
    "status": sys.argv[3],
    "detail": sys.argv[4],
    "updated_at_utc": sys.argv[5],
})
PY
  fi
}

if ! command -v nginx >/dev/null 2>&1; then
  # Fast-path when already recorded as not_installed (avoids spawning python3 on every apt run)
  if [ -f "${STATE_FILE}" ] && grep -q '"not_installed"' "${STATE_FILE}" 2>/dev/null; then
    exit 0
  fi
  echo "[INFO] Nginx is not installed; AstroDrive Web UI runs standalone on port 8090."
  write_status "not_installed" "Nginx not installed; use port 8090."
  exit 0
fi

if [ ! -f "${SNIPPET_PATH}" ]; then
  echo "[ERROR] Missing Nginx overlay snippet: ${SNIPPET_PATH}" >&2
  write_status "failed" "Missing snippet ${SNIPPET_PATH}"
  exit 1
fi

# Find Astroberry / default Nginx server config containing a `server {` block
TARGET_CONF=""
for candidate in \
  /etc/nginx/sites-enabled/astroberry* \
  /etc/nginx/conf.d/astroberry*.conf \
  /etc/nginx/sites-enabled/default \
  /etc/nginx/conf.d/default.conf; do
  if [ -f "${candidate}" ] && grep -qE '^\s*server\s*\{' "${candidate}"; then
    TARGET_CONF="$(readlink -f "${candidate}")"
    break
  fi
done

if [ -z "${TARGET_CONF}" ]; then
  echo "[WARNING] Nginx is installed but no Astroberry/default server block was found; use port 8090." >&2
  write_status "no_server_block" "No Astroberry/default Nginx server block found; use port 8090."
  exit 2
fi

# #15: Grep fast-path — when already attached and status JSON already reflects it, skip python3!
if grep -qF "${SNIPPET_PATH}" "${TARGET_CONF}"; then
  if [ -f "${STATE_FILE}" ] && grep -q '"attached"' "${STATE_FILE}" 2>/dev/null; then
    exit 0
  fi
  echo "==> Astroberry Nginx overlay already attached to ${TARGET_CONF}."
  write_status "attached" "Attached to ${TARGET_CONF}"
  exit 0
fi

BACKUP="${TARGET_CONF}.onstep.bak"
cp -a "${TARGET_CONF}" "${BACKUP}"

# Insert include into ONLY the first matching `server {` block (count=1)
if ! python3 - "${TARGET_CONF}" "${INCLUDE_LINE}" <<'PY'
import pathlib, re, sys

conf_path = pathlib.Path(sys.argv[1])
include_line = sys.argv[2]
text = conf_path.read_text(encoding="utf-8")

new_text, count = re.subn(
    r"(^\s*server\s*\{\s*$)",
    r"\1\n" + include_line,
    text,
    count=1,
    flags=re.MULTILINE,
)
if count == 0:
    sys.exit(1)
conf_path.write_text(new_text, encoding="utf-8")
PY
then
  mv -f "${BACKUP}" "${TARGET_CONF}"
  echo "[ERROR] Could not insert overlay include into ${TARGET_CONF}." >&2
  write_status "failed" "Could not insert include into ${TARGET_CONF}"
  exit 1
fi

if nginx -t >/dev/null 2>&1; then
  rm -f "${BACKUP}"
  echo "==> Attached Astroberry Nginx overlay to ${TARGET_CONF} (nginx -t passed)."
  write_status "attached" "Attached to ${TARGET_CONF}"
  if ! (command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --chroot >/dev/null 2>&1); then
    systemctl reload nginx >/dev/null 2>&1 || true
  fi
else
  echo "[ERROR] nginx -t failed after attaching overlay to ${TARGET_CONF}; rolled back cleanly." >&2
  mv -f "${BACKUP}" "${TARGET_CONF}"
  write_status "failed" "nginx -t failed on ${TARGET_CONF} (rolled back); use port 8090."
  exit 1
fi
