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
status_path = Path(sys.argv[7])

destination = Path(output)
staging = destination.with_name(destination.name + ".partial")
label = str(frame_count) if frame_count else "live"


def render(mean):
    flat = mean.reshape(-1, 3)
    # a seventh of the pixels fixes the median well inside a step and keeps the per-frame cost low
    # enough to redraw the preview between captures on a Pi
    sample = flat[::7]
    # most of a sky frame is background, so its median is the floor to remove before amplifying
    black = np.median(sample, axis=0)
    # the stream is MJPEG and its 8x8 block structure repeats in every frame, so averaging never
    # removes it; clipping at the background spread stops the stretch from amplifying it into a mesh
    spread = np.median(np.abs(sample - black), axis=0)
    floor = black + 2.0 * spread
    signal = np.clip(mean - floor, 0.0, None)
    applied = gain
    if applied <= 0:
        # put the brightest real detail just under white; beyond that gain only trades detail for
        # clipping, which is why a hand-picked number either does nothing or washes the frame out
        highlight = float(np.percentile(signal.reshape(-1, 3)[::7], 99.5))
        applied = min(64.0, 230.0 / max(highlight, 1e-6))
    signal = signal * applied
    if gamma != 1.0:
        signal = 255.0 * np.power(np.clip(signal / 255.0, 0.0, 1.0), 1.0 / gamma)
    return Image.fromarray(np.clip(signal, 0.0, 255.0).astype(np.uint8)), float(floor.mean()), applied


total = None
index = 0
while frame_count == 0 or index < frame_count:
    with urlopen(snapshot_url, timeout=10) as response:
        frame = np.asarray(Image.open(io.BytesIO(response.read())).convert("RGB"), dtype=np.float32)
    # an 8-bit running mean rounds away the sub-step detail that stacking exists to recover
    total = frame if total is None else total + frame
    index += 1
    mean = total / index
    result, floor, applied = render(mean)
    result.save(staging, format="JPEG", quality=92)
    # swap in one step so the web server never serves a half-written frame
    staging.replace(destination)
    # the preview is republished every frame, so this reports progress rather than a final summary
    status_path.write_text(
        f"{index}/{label} frames | input mean {mean.mean():.1f} peak {mean.max():.0f} of 255"
        f" | floor {floor:.1f} | gain {applied:.3g}{'' if gain > 0 else ' auto'} gamma {gamma:g}"
    )
    if frame_count == 0 or index < frame_count:
        time.sleep(interval_ms / 1000)
