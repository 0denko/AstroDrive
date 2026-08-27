#!/usr/bin/env python3
import io
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from PIL import Image, ImageChops, ImageEnhance, ImageStat

output, snapshot_url, frame_count, interval_ms, stretch = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
frames = []
for _ in range(frame_count):
    with urlopen(snapshot_url, timeout=10) as response:
        frames.append(Image.open(io.BytesIO(response.read())).convert("RGB"))
    time.sleep(interval_ms / 1000)

width, height = frames[0].size
accumulator = [0.0] * (width * height * 3)
for frame in frames:
    pixels = list(frame.getdata())
    for index, pixel in enumerate(pixels):
        offset = index * 3
        accumulator[offset] += pixel[0]
        accumulator[offset + 1] += pixel[1]
        accumulator[offset + 2] += pixel[2]
result = Image.new("RGB", (width, height))
result.putdata([tuple(min(255, int(accumulator[index * 3 + channel] / frame_count * stretch)) for channel in range(3)) for index in range(width * height)])
result.save(output, quality=92)