#!/usr/bin/env bash
# Configures NetworkManager on Raspberry Pi OS (Bookworm/Trixie) or Astroberry OS v3.2+
# for seamless Home Wi-Fi (high priority) + automatic Dark-Sky Field Hotspot fallback.
#
# Writes 0600 NetworkManager keyfiles directly and calls `nmcli connection reload`
# so Wi-Fi passwords NEVER appear in `nmcli` argv (/proc/<pid>/cmdline).
#
# Usage:
#   ASTRODRIVE_AP_PASS="<your-8+-char-password>" sudo -E ./scripts/setup_hotspot.sh --ap-ssid "AstroPi-OnStep" \
#       [--home-ssid "MyHomeWiFi"]   # (optional: pass ASTRODRIVE_HOME_PASS via env)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Please run with sudo: sudo $0 ..." >&2
  exit 1
fi

if ! command -v nmcli >/dev/null 2>&1; then
  echo "[ERROR] NetworkManager (nmcli) is required (standard on RPi OS Bookworm/Trixie & Astroberry OS 3.x)." >&2
  exit 1
fi

IFACE="${IFACE:-wlan0}"
AP_CON_NAME="${AP_CON_NAME:-astropi-hotspot}"
AP_SSID="${AP_SSID:-AstroPi-OnStep}"
AP_PASS="${ASTRODRIVE_AP_PASS:-${AP_PASS:-}}"
AP_IP="${AP_IP:-10.42.0.1/24}"
HOME_SSID="${HOME_SSID:-}"
HOME_PASS="${ASTRODRIVE_HOME_PASS:-${HOME_PASS:-}}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iface) IFACE="$2"; shift 2 ;;
    --ap-ssid) AP_SSID="$2"; shift 2 ;;
    --ap-pass) AP_PASS="$2"; shift 2 ;;
    --ap-ip) AP_IP="$2"; shift 2 ;;
    --home-ssid) HOME_SSID="$2"; shift 2 ;;
    --home-pass) HOME_PASS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ "${#AP_PASS}" -lt 8 ]; then
  echo "[ERROR] Field hotspot password must be at least 8 characters for WPA2-PSK." >&2
  exit 1
fi
if [ -n "${HOME_SSID}" ] && [ "${#HOME_PASS}" -lt 8 ]; then
  echo "[ERROR] Home Wi-Fi password must be at least 8 characters." >&2
  exit 1
fi

NM_DIR="/etc/NetworkManager/system-connections"
mkdir -p "${NM_DIR}"
umask 0077

write_nm_keyfile() {
  local dest="$1"
  local content="$2"
  local tmp="${dest}.tmp.$$"
  printf '%s\n' "${content}" > "${tmp}"
  chmod 0600 "${tmp}"
  mv -f "${tmp}" "${dest}"
}

if [ -n "${HOME_SSID}" ]; then
  echo "==> Configuring high-priority Home Wi-Fi profile: ${HOME_SSID} (priority 100)..."
  nmcli connection delete "home-wifi" >/dev/null 2>&1 || true
  HOME_UUID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')"
  write_nm_keyfile "${NM_DIR}/home-wifi.nmconnection" "[connection]
id=home-wifi
uuid=${HOME_UUID}
type=wifi
interface-name=${IFACE}
autoconnect=true
autoconnect-priority=100
autoconnect-retries=3

[wifi]
mode=infrastructure
ssid=${HOME_SSID}

[wifi-security]
key-mgmt=wpa-psk
psk=${HOME_PASS}

[ipv4]
method=auto

[ipv6]
method=auto"
fi

echo "==> Configuring automatic Dark-Sky Hotspot fallback: ${AP_SSID} (priority 10, IP ${AP_IP})..."
nmcli connection delete "${AP_CON_NAME}" >/dev/null 2>&1 || true
AP_UUID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')"
write_nm_keyfile "${NM_DIR}/${AP_CON_NAME}.nmconnection" "[connection]
id=${AP_CON_NAME}
uuid=${AP_UUID}
type=wifi
interface-name=${IFACE}
autoconnect=true
autoconnect-priority=10

[wifi]
band=bg
mode=ap
ssid=${AP_SSID}

[wifi-security]
key-mgmt=wpa-psk
psk=${AP_PASS}

[ipv4]
address1=${AP_IP}
method=shared

[ipv6]
method=ignore"

nmcli connection reload

echo ""
echo "==> Done! NetworkManager behavior:"
echo "    • At home: connects automatically to Home Wi-Fi (priority 100)."
echo "    • At dark-sky sites (no home Wi-Fi): automatically broadcasts '${AP_SSID}'."
echo "    • Connect your phone to '${AP_SSID}' and open http://$(hostname).local or http://${AP_IP%/*}"
