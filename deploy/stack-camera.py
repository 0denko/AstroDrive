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


# where the sky is placed on the output scale; a lower value keeps the night looking like night
# while still leaving the background above black, so faint detail has somewhere to show
BACKGROUND = 0.15


def finish(values, black_point, description):
    if gamma != 1.0:
        values = 255.0 * np.power(np.clip(values / 255.0, 0.0, 1.0), 1.0 / gamma)
    image = Image.fromarray(np.clip(values, 0.0, 255.0).astype(np.uint8))
    return image, float(np.mean(black_point)), description


def render(mean):
    flat = mean.reshape(-1, 3)
    # a seventh of the pixels fixes the median well inside a step and keeps the per-frame cost low
    # enough to redraw the preview between captures on a Pi
    sample = flat[::7]
    # most of a sky frame is background, so its median is the level to build the stretch around
    black = np.median(sample, axis=0)
    # the stream is MJPEG and its 8x8 block structure repeats in every frame, so averaging never
    # removes it; the spread of the background is the scale that says what counts as noise
    spread = np.median(np.abs(sample - black), axis=0)
    if gain > 0:
        # a fixed multiplier magnifies the background along with the subject, so the background has
        # to be cut away first or the stretch turns the block structure into a mesh
        floor = black + 2.0 * spread
        return finish(np.clip(mean - floor, 0.0, None) * gain, floor, f"gain {gain:g}")
    # scaling until the brightest pixels reach white lets one saturated object decide the whole
    # frame, which is why a lit window or a bright planet left everything else near black. Anchor
    # on the background instead: drop what sits below the noise, then bend the curve so the sky
    # lands on a fixed grey. Nothing here depends on how bright the scene happens to be.
    shadow = np.clip(black - 2.8 * spread, 0.0, None)
    span = np.maximum(255.0 - shadow, 1e-6)
    x = np.clip((mean - shadow) / span, 0.0, 1.0)
    sky = np.clip((black - shadow) / span, 1e-6, 1.0 - 1e-6)
    # the midtone that maps sky to BACKGROUND, solved from the transfer function rather than searched
    midtone = sky * (BACKGROUND - 1.0) / (2.0 * sky * BACKGROUND - sky - BACKGROUND)
    curved = 255.0 * ((midtone - 1.0) * x) / ((2.0 * midtone - 1.0) * x - midtone)
    return finish(curved, shadow, f"midtone {np.mean(midtone):.3g} auto")


total = None
index = 0
while frame_count == 0 or index < frame_count:
    with urlopen(snapshot_url, timeout=10) as response:
        frame = np.asarray(Image.open(io.BytesIO(response.read())).convert("RGB"), dtype=np.float32)
    # an 8-bit running mean rounds away the sub-step detail that stacking exists to recover
    total = frame if total is None else total + frame
    index += 1
    mean = total / index
    result, black_point, applied = render(mean)
    result.save(staging, format="JPEG", quality=92)
    # swap in one step so the web server never serves a half-written frame
    staging.replace(destination)
    # the preview is republished every frame, so this reports progress rather than a final summary
    status_path.write_text(
        f"{index}/{label} frames | input mean {mean.mean():.1f} peak {mean.max():.0f} of 255"
        f" | black {black_point:.1f} | {applied} | gamma {gamma:g}"
    )
    if frame_count == 0 or index < frame_count:
        time.sleep(interval_ms / 1000)
