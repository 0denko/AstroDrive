#!/usr/bin/env bash
set -Eeuo pipefail

streamer="$(command -v mjpg_streamer || true)"
streamer="${streamer:-$(find /opt/mjpg-streamer -type f -name mjpg_streamer -executable -print -quit)}"
input_plugin="$(find /usr /opt -name input_uvc.so -type f -print -quit)"
output_plugin="$(find /usr /opt -name output_http.so -type f -print -quit)"
camera_device="${CAMERA_DEVICE:-auto}"
if [[ ! -x "$streamer" || -z "$input_plugin" || -z "$output_plugin" ]]; then
  echo "mjpg-streamer plugins were not found"
  exit 1
fi
if [[ "$camera_device" == "auto" ]]; then
  # a Pi exposes codec and ISP nodes under /dev/video* too, so pick the first that can capture
  for candidate in /dev/video*; do
    if [[ -e "$candidate" ]] && v4l2-ctl --device "$candidate" --list-formats 2>/dev/null | grep -q '^\s*\[0\]'; then
      camera_device="$candidate"
      break
    fi
  done
fi
if [[ "$camera_device" == "auto" || ! -e "$camera_device" ]]; then
  echo "USB camera was not found (tried ${CAMERA_DEVICE:-auto})"
  exit 1
fi

input_options="-d $camera_device -r 640x480 -f 10"
# the camera's own MJPEG quantises hard, and its 8x8 blocks survive stacking because every frame
# blocks identically; -y captures YUYV and re-encodes here instead, at the cost of CPU
if [[ -n "${CAMERA_QUALITY:-}" ]]; then
  input_options="$input_options -y -q $CAMERA_QUALITY"
fi

exec "$streamer" \
  -i "$input_plugin $input_options" \
  -o "$output_plugin -w /usr/share/mjpg-streamer/www -p 8080"
