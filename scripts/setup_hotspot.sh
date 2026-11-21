#!/usr/bin/env bash
# Configures NetworkManager on Raspberry Pi OS (Bookworm/Trixie) or Astroberry OS v3.2+
# for seamless Home Wi-Fi (high priority) + automatic Dark-Sky Field Hotspot fallback.
#
# Usage:
#   sudo ./scripts/setup_hotspot.sh --ap-ssid "AstroPi-OnStep" --ap-pass "clearSkies2026" \
#       [--home-ssid "MyHomeWiFi" --home-pass "MyHomePass"]
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
AP_PASS="${AP_PASS:-astropi1234}"
AP_IP="${AP_IP:-10.42.0.1/24}"
HOME_SSID=""
HOME_PASS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iface) IFACE="$2"; shift 2 ;;
    --ap-ssid) AP_SSID="$2"; shift 2 ;;
    --ap-pass) AP_PASS="$2"; shift 2 ;;
    --ap-ip) AP_IP="$2"; shift 2 ;;
    --home-ssid) HOME_SSID="$2"; shift 2 ;;
    --home-pass) HOME_PASS="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ "${#AP_PASS}" -lt 8 ]; then
  echo "[ERROR] --ap-pass must be at least 8 characters for WPA2-PSK." >&2
  exit 1
fi

if [ -n "${HOME_SSID}" ]; then
  echo "==> Configuring high-priority Home Wi-Fi profile: ${HOME_SSID} (priority 100)..."
  nmcli connection delete "home-wifi" >/dev/null 2>&1 || true
  nmcli connection add type wifi ifname "${IFACE}" con-name "home-wifi" ssid "${HOME_SSID}" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${HOME_PASS}" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    connection.autoconnect-retries 3
fi

echo "==> Configuring automatic Dark-Sky Hotspot fallback: ${AP_SSID} (priority 10, IP ${AP_IP})..."
nmcli connection delete "${AP_CON_NAME}" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "${IFACE}" con-name "${AP_CON_NAME}" autoconnect yes ssid "${AP_SSID}" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  ipv4.addresses "${AP_IP}" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "${AP_PASS}" \
  connection.autoconnect-priority 10

echo ""
echo "==> Done! NetworkManager behavior:"
echo "    • At home: connects automatically to Home Wi-Fi (priority 100)."
echo "    • At dark-sky sites (no home Wi-Fi): automatically broadcasts '${AP_SSID}'."
echo "    • Connect your phone to '${AP_SSID}' and open http://$(hostname).local or http://${AP_IP%/*}"
