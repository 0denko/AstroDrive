#!/usr/bin/env bash
# Safely attaches /etc/nginx/snippets/onstep-astroberry-overlay.conf to Astroberry OS's
# Nginx server block (idempotent, validated with `nginx -t` with automatic rollback).
#
# Also called automatically via /etc/apt/apt.conf.d/99-onstep-nginx-overlay so `apt upgrade`
# of Astroberry packages never drops the UI overlay.
set -euo pipefail

SNIPPET_PATH="/etc/nginx/snippets/onstep-astroberry-overlay.conf"
INCLUDE_LINE="    include ${SNIPPET_PATH};"

if [ ! -f "${SNIPPET_PATH}" ] || ! command -v nginx >/dev/null 2>&1; then
  exit 0
fi

# Find Astroberry / default Nginx server config containing a `server {` block
TARGET_CONF=""
for candidate in \
  /etc/nginx/sites-enabled/astroberry* \
  /etc/nginx/sites-enabled/default \
  /etc/nginx/conf.d/astroberry*.conf \
  /etc/nginx/conf.d/default.conf; do
  if [ -f "${candidate}" ] && grep -qE '^\s*server\s*\{' "${candidate}"; then
    TARGET_CONF="$(readlink -f "${candidate}")"
    break
  fi
done

if [ -z "${TARGET_CONF}" ]; then
  echo "[INFO] No Nginx server block found to attach Astroberry overlay (standalone UI still available on port 8090)."
  exit 0
fi

if grep -qF "${SNIPPET_PATH}" "${TARGET_CONF}"; then
  echo "==> Astroberry Nginx overlay already attached to ${TARGET_CONF}."
  exit 0
fi

BACKUP="${TARGET_CONF}.onstep.bak"
cp -a "${TARGET_CONF}" "${BACKUP}"

python3 - "${TARGET_CONF}" "${INCLUDE_LINE}" <<'PY'
import pathlib, re, sys

conf_path = pathlib.Path(sys.argv[1])
include_line = sys.argv[2]
text = conf_path.read_text(encoding="utf-8")

# Insert include inside each `server {` block (immediately after `server {`)
new_text, count = re.subn(
    r"(^\s*server\s*\{\s*$)",
    r"\1\n" + include_line,
    text,
    flags=re.MULTILINE,
)
if count == 0:
    sys.exit(1)
conf_path.write_text(new_text, encoding="utf-8")
PY

if nginx -t >/dev/null 2>&1; then
  rm -f "${BACKUP}"
  echo "==> Attached Astroberry Nginx overlay to ${TARGET_CONF} (nginx -t passed)."
  if ! (command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt --chroot >/dev/null 2>&1); then
    systemctl reload nginx >/dev/null 2>&1 || true
  fi
else
  echo "[WARNING] nginx -t failed after attaching overlay to ${TARGET_CONF}; rolling back cleanly." >&2
  mv -f "${BACKUP}" "${TARGET_CONF}"
  exit 0
fi
