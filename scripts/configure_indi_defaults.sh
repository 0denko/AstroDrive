#!/usr/bin/env bash
# Pre-configures INDI's `LX200 OnStep` driver (`~/.indi/LX200 OnStep_config.xml`)
# so KStars/Ekos defaults to Serial /dev/onstep @ 9600 baud with Auto Search disabled.
#
# Usage:
#   sudo ./scripts/configure_indi_defaults.sh           # Only create if missing
#   sudo ./scripts/configure_indi_defaults.sh --force   # Overwrite existing config
set -euo pipefail

FORCE="false"
if [ "${1:-}" = "--force" ]; then
  FORCE="true"
fi

write_indi_config_for_user() {
  local username="$1"
  if ! id "${username}" >/dev/null 2>&1; then
    return 0
  fi

  local home_dir
  home_dir="$(getent passwd "${username}" | cut -d: -f6)"
  if [ -z "${home_dir}" ] || [ ! -d "${home_dir}" ]; then
    return 0
  fi

  local indi_dir="${home_dir}/.indi"
  local cfg_file="${indi_dir}/LX200 OnStep_config.xml"

  if [ -f "${cfg_file}" ] && [ "${FORCE}" != "true" ]; then
    echo "==> Preserving existing INDI config: ${cfg_file}"
    return 0
  fi

  mkdir -p "${indi_dir}"
  cat > "${cfg_file}" <<'EOF'
<INDIDriver>
<newSwitchVector device='LX200 OnStep' name='CONNECTION_MODE'>
  <oneSwitch name='CONNECTION_SERIAL'>On</oneSwitch>
  <oneSwitch name='CONNECTION_TCP'>Off</oneSwitch>
</newSwitchVector>
<newTextVector device='LX200 OnStep' name='DEVICE_PORT'>
  <oneText name='PORT'>/dev/onstep</oneText>
</newTextVector>
<newSwitchVector device='LX200 OnStep' name='DEVICE_BAUD_RATE'>
  <oneSwitch name='1200'>Off</oneSwitch>
  <oneSwitch name='2400'>Off</oneSwitch>
  <oneSwitch name='4800'>Off</oneSwitch>
  <oneSwitch name='9600'>On</oneSwitch>
  <oneSwitch name='19200'>Off</oneSwitch>
  <oneSwitch name='38400'>Off</oneSwitch>
  <oneSwitch name='57600'>Off</oneSwitch>
  <oneSwitch name='115200'>Off</oneSwitch>
  <oneSwitch name='230400'>Off</oneSwitch>
</newSwitchVector>
<newSwitchVector device='LX200 OnStep' name='DEVICE_AUTO_SEARCH'>
  <oneSwitch name='INDI_ENABLED'>Off</oneSwitch>
  <oneSwitch name='INDI_DISABLED'>On</oneSwitch>
</newSwitchVector>
</INDIDriver>
EOF

  local grp
  grp="$(id -gn "${username}")"
  chown -R "${username}:${grp}" "${indi_dir}"
  echo "==> Configured INDI defaults for '${username}': ${cfg_file} (/dev/onstep @ 9600 baud)"
}

# Collect target users (deduplicated): astroberry, pi, SUDO_USER, and any login user in /home
declare -A SEEN_USERS=()
CANDIDATES=(astroberry pi)
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  CANDIDATES+=("${SUDO_USER}")
fi
if [ -d /home ]; then
  for d in /home/*; do
    [ -d "${d}" ] && CANDIDATES+=("$(basename "${d}")")
  done
fi

for u in "${CANDIDATES[@]}"; do
  if [ -z "${SEEN_USERS[${u}]:-}" ]; then
    SEEN_USERS["${u}"]=1
    write_indi_config_for_user "${u}"
  fi
done
