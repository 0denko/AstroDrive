#!/usr/bin/env python3
import io
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageEnhance

output, snapshot_url, frame_count, interval_ms, stretch = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
result = None
for index in range(1, frame_count + 1):
    with urlopen(snapshot_url, timeout=10) as response:
        frame = Image.open(io.BytesIO(response.read())).convert("RGB")
    # running mean, so only two frames are ever held in memory
    result = frame if result is None else Image.blend(result, frame, 1.0 / index)
    if index < frame_count:
        time.sleep(interval_ms / 1000)

if stretch != 1.0:
    result = ImageEnhance.Brightness(result).enhance(stretch)
destination = Path(output)
staging = destination.with_name(destination.name + ".partial")
result.save(staging, format="JPEG", quality=92)
# swap in one step so the web server never serves a half-written frame
staging.replace(destination)
