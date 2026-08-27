#!/usr/bin/env python3
import io
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import numpy as np
from PIL import Image

output, snapshot_url = sys.argv[1], sys.argv[2]
frame_count, interval_ms = int(sys.argv[3]), int(sys.argv[4])
gain, gamma = float(sys.argv[5]), float(sys.argv[6])

total = None
for index in range(1, frame_count + 1):
    with urlopen(snapshot_url, timeout=10) as response:
        frame = np.asarray(Image.open(io.BytesIO(response.read())).convert("RGB"), dtype=np.float32)
    # an 8-bit running mean rounds away the sub-step detail that stacking exists to recover
    total = frame if total is None else total + frame
    if index < frame_count:
        time.sleep(interval_ms / 1000)

mean = total / frame_count
# most of a sky frame is background, so its median is the floor to remove before amplifying
black = np.median(mean.reshape(-1, 3), axis=0)
# the stream is MJPEG and its 8x8 block structure repeats in every frame, so averaging never
# removes it; clipping at the background spread stops the stretch from amplifying it into a mesh
spread = np.median(np.abs(mean.reshape(-1, 3) - black), axis=0)
floor = black + 2.0 * spread
signal = np.clip(mean - floor, 0.0, None) * gain
if gamma != 1.0:
    signal = 255.0 * np.power(np.clip(signal / 255.0, 0.0, 1.0), 1.0 / gamma)
result = Image.fromarray(np.clip(signal, 0.0, 255.0).astype(np.uint8))

destination = Path(output)
staging = destination.with_name(destination.name + ".partial")
result.save(staging, format="JPEG", quality=92)
# swap in one step so the web server never serves a half-written frame
staging.replace(destination)
print(f"{frame_count} frames | input mean {mean.mean():.1f} peak {mean.max():.0f} of 255 | floor {floor.mean():.1f} | gain {gain:g} gamma {gamma:g}")
