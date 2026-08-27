#!/usr/bin/env bash
set -Eeuo pipefail

input_plugin="$(find /usr -name input_uvc.so -type f -print -quit)"
output_plugin="$(find /usr -name output_http.so -type f -print -quit)"
camera_device="${CAMERA_DEVICE:-auto}"
if [[ -z "$input_plugin" || -z "$output_plugin" ]]; then
  echo "mjpg-streamer plugins were not found"
  exit 1
fi
if [[ "$camera_device" == "auto" ]]; then
  for candidate in /dev/video*; do
    if [[ -e "$candidate" ]]; then
      camera_device="$candidate"
      break
    fi
  done
fi
if [[ "$camera_device" == "auto" || ! -e "$camera_device" ]]; then
  echo "USB camera was not found (tried ${CAMERA_DEVICE:-auto})"
  exit 1
fi

exec /usr/bin/mjpg_streamer \
  -i "$input_plugin -d $camera_device -r 640x480 -f 10" \
  -o "$output_plugin -w /usr/share/mjpg-streamer/www -p 8080"