#!/usr/bin/env bash
set -Eeuo pipefail

input_plugin="$(find /usr -name input_uvc.so -type f -print -quit)"
output_plugin="$(find /usr -name output_http.so -type f -print -quit)"
if [[ -z "$input_plugin" || -z "$output_plugin" ]]; then
  echo "mjpg-streamer plugins were not found"
  exit 1
fi
if [[ ! -e /dev/video0 ]]; then
  echo "USB camera /dev/video0 was not found"
  exit 1
fi

exec /usr/bin/mjpg_streamer \
  -i "$input_plugin -d /dev/video0 -r 640x480 -f 10" \
  -o "$output_plugin -w /usr/share/mjpg-streamer/www -p 8080"