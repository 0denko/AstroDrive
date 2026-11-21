#!/usr/bin/env python3
"""
AstroDrive USB Camera Capture & EAA Live Stacking Daemon (GPLv3).

Designed for Raspberry Pi 3 Model B (1 GB RAM, Cortex-A53, USB 2.0) + UVC webcams
(e.g., disassembled Logitech USB webcam module):
  - Captures YUYV frames via OpenCV V4L2 at modest resolution (default 640x480)
    so MJPEG compression artefacts never poison sharpness grading or stacking.
  - Enumerates, sets, and reads back real V4L2 controls via `v4l2-ctl` (including
    "Lock Auto Controls" to disable auto-exposure, auto-gain, auto-WB, autofocus).
  - Arbitrates /dev/video* cleanly with INDI (`indi_v4l2_ccd`) via onstep_common
    so it never crash-loops when INDI owns the camera.
  - Runs an in-RAM EAA pipeline (zero per-frame SD card writes):
      1. Optional master dark-frame subtraction (hot-pixel removal).
      2. Per-frame quality grading on 2x2 binned mono copy:
         - Star FWHM + star-count collapse check when >=3 stars are detected
         - PSS-style Gaussian-blurred Laplacian variance fallback (Moon/planets)
         - Rolling 200-frame AutoStakkert-style Keep Best % selector + cloud floor
      3. Continuous drift auto-alignment on aggressively binned (<=320x240) mono copy
         via sub-pixel phase correlation:
         - `dynamic_drift` mode (OpenLiveStacker v34 non-tracking style: exponential
           decay average anchored to live field, auto-reset + settle-skip on bumps)
         - `fixed_ref` mode (fixed reference frame + Welford sigma-clipped average,
           cumulative drift tracking, stops & prompts Recentre beyond 25% drift)
      4. Black/white-point + asinh display stretch written atomically to tmpfs
         (`/run/onstep-camera/preview.jpg`).
      5. User-triggered Save writes stretched PNG + JSON metadata to captures dir.
"""

from __future__ import annotations

import collections
import datetime as dt
import glob
import io
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from onstep_common import find_device_holders, read_json_file, write_json_atomic  # noqa: E402

try:
    import cv2  # type: ignore
except ImportError:
    cv2 = None  # type: ignore

try:
    from PIL import Image  # type: ignore
except ImportError:
    Image = None  # type: ignore

LOG = logging.getLogger("onstep-camera")

STATE_DIR = Path(os.environ.get("ONSTEP_STATE_DIR", "/var/lib/onstep-autoprovision"))
STATUS_FILE = STATE_DIR / "camera-status.json"
CMD_DIR = STATE_DIR / "camera-cmd"
CMD_CONFIG_FILE = CMD_DIR / "config.json"
CAPTURES_DIR = STATE_DIR / "captures"
PREVIEW_DIR = Path(os.environ.get("CAMERA_PREVIEW_DIR", "/run/onstep-camera"))
PREVIEW_FILE = PREVIEW_DIR / "preview.jpg"

SUPPORTED_RESOLUTIONS = {
    "320x240": (320, 240),
    "640x480": (640, 480),
    "800x600": (800, 600),
    "1280x720": (1280, 720),
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "stacking": True,
    "mode": "dynamic_drift",      # "dynamic_drift" (untracked live) | "fixed_ref" (deep stack)
    "keep_pct": 50,               # 25 | 50 | 75 | 100
    "resolution": "640x480",      # Pi 3B baseline default (YUYV)
    "stretch_asinh": 4.0,         # 0.0 (linear) .. 20.0 (strong EAA stretch)
    "black_point": 0.02,          # 0.0 .. 0.5
    "white_point": 0.98,          # 0.2 .. 1.0
    "sigma_clip": 2.8,            # sigma threshold once >= 6 frames in fixed_ref
    "decay_frames": 24,           # effective window in dynamic_drift mode
    "drift_limit_pct": 25.0,      # max cumulative drift % in fixed_ref before prompt
    "min_corr_response": 0.08,    # phase-correlation confidence floor
    "settle_skip_frames": 3,      # frames to skip after a sudden mount bump/nudge
    "quality_floor": 0.5,         # absolute minimum sharpness score (cloud rejection)
}


# ---------------------------------------------------------------------------
# Low-level image primitives (OpenCV fast path + pure-NumPy fallback)
# ---------------------------------------------------------------------------

def to_binned_mono(frame_rgb: np.ndarray, max_dim: int = 320) -> tuple[np.ndarray, float]:
    """Return (mono_float32, scale_factor) downscaled so max(h, w) <= max_dim."""
    h, w = frame_rgb.shape[:2]
    if frame_rgb.ndim == 3:
        if cv2 is not None:
            mono = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        else:
            mono = (
                0.299 * frame_rgb[:, :, 0].astype(np.float32)
                + 0.587 * frame_rgb[:, :, 1].astype(np.float32)
                + 0.114 * frame_rgb[:, :, 2].astype(np.float32)
            )
    else:
        mono = frame_rgb.astype(np.float32)

    factor = 1
    while max(h // factor, w // factor) > max_dim and (h // (factor * 2)) >= 64 and (w // (factor * 2)) >= 64:
        factor *= 2

    if factor > 1:
        nh, nw = (h // factor) * factor, (w // factor) * factor
        mono = mono[:nh, :nw].reshape(nh // factor, factor, nw // factor, factor).mean(axis=(1, 3))
    return mono.astype(np.float32), float(factor)


def _gaussian_blur_3x3(img: np.ndarray) -> np.ndarray:
    if cv2 is not None:
        return cv2.GaussianBlur(img, (5, 5), 1.0)
    # Separable 1-2-1 blur in pure numpy
    p = np.pad(img, 1, mode="edge")
    h = 0.25 * p[:, :-2] + 0.5 * p[:, 1:-1] + 0.25 * p[:, 2:]
    return 0.25 * h[:-2, :] + 0.5 * h[1:-1, :] + 0.25 * h[2:, :]


def _detect_stars_and_fwhm(mono: np.ndarray) -> tuple[int, float]:
    """Fast lightweight star detector on binned mono image -> (star_count, median_fwhm_px)."""
    med = float(np.median(mono))
    mad = float(np.median(np.abs(mono - med)))
    sigma = max(1.4826 * mad, 1.5)
    thresh = med + 6.0 * sigma
    mask = mono > thresh
    if not np.any(mask):
        return 0, 0.0

    # Local 3x3 peak finding
    p = np.pad(mono, 1, mode="constant", constant_values=0.0)
    center = p[1:-1, 1:-1]
    is_peak = (
        mask
        & (center >= p[:-2, :-2]) & (center >= p[:-2, 1:-1]) & (center >= p[:-2, 2:])
        & (center >= p[1:-1, :-2]) & (center >= p[1:-1, 2:])
        & (center >= p[2:, :-2]) & (center >= p[2:, 1:-1]) & (center >= p[2:, 2:])
    )
    ys, xs = np.nonzero(is_peak)
    if len(ys) == 0:
        return 0, 0.0

    # Keep top 40 brightest peaks away from edges
    h, w = mono.shape
    vals = mono[ys, xs]
    order = np.argsort(vals)[::-1][:40]
    fwhms: list[float] = []
    r = 4
    for idx in order:
        y, x = int(ys[idx]), int(xs[idx])
        if y < r or y >= h - r or x < r or x >= w - r:
            continue
        patch = mono[y - r : y + r + 1, x - r : x + r + 1] - med
        patch = np.clip(patch, 0.0, None)
        total = float(patch.sum())
        if total <= 0.0:
            continue
        peak = float(patch[r, r])
        # Ignore single hot pixels (peak dominates >85% of 9x9 flux)
        if peak / total > 0.85:
            continue
        yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
        cy = float((yy * patch).sum() / total)
        cx = float((xx * patch).sum() / total)
        var = float((((yy - cy) ** 2 + (xx - cx) ** 2) * patch).sum() / total)
        sigma_star = math.sqrt(max(var * 0.5, 0.15))
        fwhm = 2.355 * sigma_star
        if 1.0 <= fwhm <= 12.0:
            fwhms.append(fwhm)

    if not fwhms:
        return 0, 0.0
    return len(fwhms), float(np.median(fwhms))


def grade_frame(mono: np.ndarray) -> dict[str, Any]:
    """
    Grade sharpness on a binned mono frame:
      1. Star-based (Siril model): if >= 3 stars found, score = 100 / median_FWHM.
      2. Blurred-Laplacian fallback (PSS model): Gaussian blur first so Logitech
         high-gain sensor noise does not inflate sharpness score.
    """
    star_count, median_fwhm = _detect_stars_and_fwhm(mono)
    if star_count >= 3 and median_fwhm > 0.0:
        return {
            "metric": "star_fwhm",
            "score": round(100.0 / median_fwhm, 3),
            "star_count": star_count,
            "median_fwhm": round(median_fwhm, 2),
        }

    blurred = _gaussian_blur_3x3(mono)
    if cv2 is not None:
        lap = cv2.Laplacian(blurred, cv2.CV_32F, ksize=3)
    else:
        p = np.pad(blurred, 1, mode="edge")
        lap = p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] - 4.0 * p[1:-1, 1:-1]
    score = float(np.var(lap))
    return {
        "metric": "laplacian",
        "score": round(score, 3),
        "star_count": star_count,
        "median_fwhm": round(median_fwhm, 2),
    }


def phase_correlate(ref_mono: np.ndarray, cur_mono: np.ndarray) -> tuple[float, float, float]:
    """Return (dx, dy, response) where shifting cur by (+dx, +dy) aligns it to ref."""
    if cv2 is not None:
        h, w = ref_mono.shape
        hann = cv2.createHanningWindow((w, h), cv2.CV_32F)
        (shift_x, shift_y), response = cv2.phaseCorrelate(cur_mono, ref_mono, hann)
        return float(shift_x), float(shift_y), float(response)

    # Pure-NumPy FFT phase correlation fallback
    h, w = ref_mono.shape
    wy = np.hanning(h).astype(np.float32)[:, None]
    wx = np.hanning(w).astype(np.float32)[None, :]
    win = wy * wx
    a = (cur_mono - float(cur_mono.mean())) * win
    b = (ref_mono - float(ref_mono.mean())) * win
    fa = np.fft.rfft2(a)
    fb = np.fft.rfft2(b)
    cps = fb * np.conj(fa)
    mag = np.abs(cps)
    cps /= np.maximum(mag, 1e-7)
    corr = np.fft.irfft2(cps, s=(h, w))
    peak_idx = int(np.argmax(corr))
    py, px = divmod(peak_idx, w)
    response = float(corr[py, px])
    # Sub-pixel parabolic refinement around peak
    dy = float(py if py <= h // 2 else py - h)
    dx = float(px if px <= w // 2 else px - w)
    return dx, dy, response


def shift_image(frame: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Translate frame by (dx, dy) pixels with border replication."""
    if abs(dx) < 0.05 and abs(dy) < 0.05:
        return frame
    if cv2 is not None:
        h, w = frame.shape[:2]
        mat = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
        return cv2.warpAffine(frame, mat, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    # Pure-NumPy integer-roll fallback with edge fill
    ix, iy = int(round(dx)), int(round(dy))
    out = np.empty_like(frame)
    h, w = frame.shape[:2]
    src_y0 = max(0, -iy)
    src_y1 = min(h, h - iy)
    src_x0 = max(0, -ix)
    src_x1 = min(w, w - ix)
    dst_y0 = max(0, iy)
    dst_y1 = dst_y0 + (src_y1 - src_y0)
    dst_x0 = max(0, ix)
    dst_x1 = dst_x0 + (src_x1 - src_x0)
    out[:] = np.median(frame, axis=(0, 1)) if frame.ndim == 3 else float(np.median(frame))
    if dst_y1 > dst_y0 and dst_x1 > dst_x0:
        out[dst_y0:dst_y1, dst_x0:dst_x1] = frame[src_y0:src_y1, src_x0:src_x1]
    return out


def stretch_for_display(
    img_float: np.ndarray,
    black_point: float = 0.02,
    white_point: float = 0.98,
    stretch_asinh: float = 4.0,
) -> np.ndarray:
    """Apply black/white normalization + asinh EAA stretch -> uint8 RGB."""
    lo = float(np.clip(black_point, 0.0, 0.85))
    hi = float(np.clip(white_point, lo + 0.05, 1.0))
    norm = np.clip(img_float / 255.0, 0.0, 1.0)
    scaled = np.clip((norm - lo) / max(hi - lo, 1e-4), 0.0, 1.0)
    if stretch_asinh > 0.05:
        k = float(stretch_asinh)
        scaled = np.arcsinh(k * scaled) / math.asinh(k)
    return np.clip(scaled * 255.0, 0.0, 255.0).astype(np.uint8)


def encode_jpeg(rgb_uint8: np.ndarray, quality: int = 82) -> bytes:
    if cv2 is not None:
        bgr = cv2.cvtColor(rgb_uint8, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if ok:
            return buf.tobytes()
    if Image is not None:
        bio = io.BytesIO()
        Image.fromarray(rgb_uint8, mode="RGB").save(bio, format="JPEG", quality=quality)
        return bio.getvalue()
    raise RuntimeError("Neither OpenCV (cv2) nor Pillow (PIL) is available to encode JPEG preview.")


def save_png(rgb_uint8: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if cv2 is not None:
        bgr = cv2.cvtColor(rgb_uint8, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(path), bgr)
        return
    if Image is not None:
        Image.fromarray(rgb_uint8, mode="RGB").save(str(path), format="PNG")
        return
    raise RuntimeError("Neither OpenCV (cv2) nor Pillow (PIL) is available to save PNG.")


# ---------------------------------------------------------------------------
# EAA LiveStacker (completely unit-testable without hardware!)
# ---------------------------------------------------------------------------

class LiveStacker:
    """
    In-RAM EAA live stacker supporting:
      - Per-frame lucky-imaging grading (star FWHM + cloud collapse OR PSS blurred-Laplacian)
      - Rolling 200-frame score history with live AutoStakkert-style Keep Best %
      - Continuous drift auto-alignment (`dynamic_drift` OLS decay OR `fixed_ref` sigma-clip)
      - Optional master dark-frame capture & subtraction
    """

    def __init__(self, settings: Optional[dict[str, Any]] = None) -> None:
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)

        self.accumulator: Optional[np.ndarray] = None
        self.m2: Optional[np.ndarray] = None
        self.ref_align_mono: Optional[np.ndarray] = None
        self.align_scale: float = 1.0
        self.dark_frame: Optional[np.ndarray] = None
        self.dark_count: int = 0
        self._dark_collecting: bool = False
        self._dark_target: int = 15
        self._dark_sum: Optional[np.ndarray] = None
        self._dark_collected: int = 0

        self.score_history: collections.deque[float] = collections.deque(maxlen=200)
        self.star_history: collections.deque[int] = collections.deque(maxlen=60)
        self.settle_skip_remaining: int = 0

        self.frames_captured: int = 0
        self.frames_kept: int = 0
        self.rejected_quality: int = 0
        self.rejected_registration: int = 0
        self.rejected_clouds: int = 0
        self.rejected_drift_limit: int = 0
        self.drift_x_px: float = 0.0
        self.drift_y_px: float = 0.0
        self.drift_limit_reached: bool = False
        self.last_score: float = 0.0
        self.last_metric: str = ""
        self.last_star_count: int = 0
        self.last_fwhm: float = 0.0
        self.current_threshold: float = 0.0
        self.last_response: float = 0.0
        self.last_frame_action: str = "idle"
        self.last_display_rgb: Optional[np.ndarray] = None

        if settings:
            self.update_settings(settings)

    def update_settings(self, updates: dict[str, Any]) -> None:
        mode_changed = "mode" in updates and updates["mode"] != self.settings.get("mode")
        for k, v in updates.items():
            if k in DEFAULT_SETTINGS:
                self.settings[k] = v
        if mode_changed:
            self.reset()

    def reset(self) -> None:
        self.accumulator = None
        self.m2 = None
        self.ref_align_mono = None
        self.score_history.clear()
        self.star_history.clear()
        self.settle_skip_remaining = 0
        self.frames_captured = 0
        self.frames_kept = 0
        self.rejected_quality = 0
        self.rejected_registration = 0
        self.rejected_clouds = 0
        self.rejected_drift_limit = 0
        self.drift_x_px = 0.0
        self.drift_y_px = 0.0
        self.drift_limit_reached = False
        self.last_frame_action = "reset"

    def start_dark_capture(self, count: int = 15) -> None:
        self._dark_collecting = True
        self._dark_target = max(3, min(int(count), 60))
        self._dark_sum = None
        self._dark_collected = 0
        self.last_frame_action = f"capturing_dark (0/{self._dark_target})"

    def clear_dark(self) -> None:
        self.dark_frame = None
        self.dark_count = 0
        self._dark_collecting = False
        self._dark_sum = None
        self._dark_collected = 0

    def _compute_threshold(self) -> float:
        keep_pct = float(np.clip(self.settings.get("keep_pct", 50), 10, 100))
        floor = float(self.settings.get("quality_floor", 0.5))
        if len(self.score_history) < 8 or keep_pct >= 99.9:
            return floor
        percentile = 100.0 - keep_pct
        dyn = float(np.percentile(list(self.score_history), percentile))
        return max(floor, dyn)

    def process_frame(self, frame_rgb_u8: np.ndarray) -> np.ndarray:
        """
        Process one RGB uint8 frame through the full EAA pipeline and return
        the stretched RGB uint8 preview image.
        """
        f32 = frame_rgb_u8.astype(np.float32)

        # Master dark collection mode (camera capped)
        if self._dark_collecting:
            if self._dark_sum is None or self._dark_sum.shape != f32.shape:
                self._dark_sum = np.zeros_like(f32, dtype=np.float32)
                self._dark_collected = 0
            self._dark_sum += f32
            self._dark_collected += 1
            self.last_frame_action = f"capturing_dark ({self._dark_collected}/{self._dark_target})"
            if self._dark_collected >= self._dark_target:
                self.dark_frame = self._dark_sum / float(self._dark_collected)
                self.dark_count = self._dark_collected
                self._dark_collecting = False
                self._dark_sum = None
                self.last_frame_action = f"dark_ready ({self.dark_count} frames)"
            self.last_display_rgb = frame_rgb_u8
            return frame_rgb_u8

        # Optional master dark subtraction
        if self.dark_frame is not None and self.dark_frame.shape == f32.shape:
            f32 = np.clip(f32 - self.dark_frame, 0.0, 255.0)

        self.frames_captured += 1

        # Reset accumulator cleanly if capture resolution changed
        if self.accumulator is not None and self.accumulator.shape != f32.shape:
            self.reset()

        # Pass-through preview if stacking is switched off
        if not self.settings.get("stacking", True):
            self.last_frame_action = "live_preview"
            disp = stretch_for_display(
                f32,
                black_point=float(self.settings["black_point"]),
                white_point=float(self.settings["white_point"]),
                stretch_asinh=float(self.settings["stretch_asinh"]),
            )
            self.last_display_rgb = disp
            return disp

        # Settle-skip period after sudden mount bump/nudge
        if self.settle_skip_remaining > 0:
            self.settle_skip_remaining -= 1
            self.last_frame_action = f"settling ({self.settle_skip_remaining} left)"
            return self._render_current(f32)

        # 1. Score BEFORE registration (on 2x2 binned mono copy)
        grade_mono, _ = to_binned_mono(f32, max_dim=320)
        g = grade_frame(grade_mono)
        self.last_score = float(g["score"])
        self.last_metric = str(g["metric"])
        self.last_star_count = int(g["star_count"])
        self.last_fwhm = float(g["median_fwhm"])

        # Cloud dropout check: if session was tracking stars and star count collapses <30%
        if len(self.star_history) >= 5:
            med_stars = float(np.median(list(self.star_history)))
            if med_stars >= 4.0 and self.last_star_count < max(2, int(0.30 * med_stars)):
                self.rejected_clouds += 1
                self.last_frame_action = "rejected_clouds"
                return self._render_current(f32)

        if self.last_star_count >= 3:
            self.star_history.append(self.last_star_count)

        self.score_history.append(self.last_score)
        self.current_threshold = round(self._compute_threshold(), 3)

        if self.last_score < self.current_threshold:
            self.rejected_quality += 1
            self.last_frame_action = "rejected_quality"
            return self._render_current(f32)

        # 2. Align on aggressively binned mono copy (<=320px, <=240px for 720p)
        align_mono, align_scale = to_binned_mono(f32, max_dim=240 if f32.shape[1] >= 1000 else 320)
        mode = str(self.settings.get("mode", "dynamic_drift"))
        h, w = f32.shape[:2]

        if self.accumulator is None or self.ref_align_mono is None:
            self.accumulator = f32.copy()
            self.m2 = np.zeros_like(f32, dtype=np.float32)
            self.ref_align_mono = align_mono
            self.align_scale = align_scale
            self.frames_kept = 1
            self.drift_x_px = 0.0
            self.drift_y_px = 0.0
            self.drift_limit_reached = False
            self.last_response = 1.0
            self.last_frame_action = "reference_acquired"
            return self._render_current(f32)

        dx_b, dy_b, response = phase_correlate(self.ref_align_mono, align_mono)
        dx = dx_b * align_scale
        dy = dy_b * align_scale
        self.last_response = round(response, 3)

        min_resp = float(self.settings.get("min_corr_response", 0.08))
        if response < min_resp:
            self.rejected_registration += 1
            self.last_frame_action = "rejected_registration"
            return self._render_current(f32)

        if mode == "dynamic_drift":
            # OpenLiveStacker v34 non-tracking mode:
            # Anchor stack to the *latest* live position by shifting the accumulator
            # by (-dx, -dy) and blending the fresh frame with exponential decay!
            jump_limit = 0.15 * min(h, w)
            if abs(dx) > jump_limit or abs(dy) > jump_limit:
                # Sudden mount nudge/bump -> reset stack and skip settle frames
                self.reset()
                self.settle_skip_remaining = int(self.settings.get("settle_skip_frames", 3))
                self.last_frame_action = "bump_reset_settling"
                return self._render_current(f32)

            shifted_acc = shift_image(self.accumulator, -dx, -dy)
            self.frames_kept += 1
            decay_n = max(4, int(self.settings.get("decay_frames", 24)))
            alpha = 1.0 / float(min(self.frames_kept, decay_n))
            self.accumulator = (1.0 - alpha) * shifted_acc + alpha * f32
            self.ref_align_mono = align_mono
            self.drift_x_px += -dx
            self.drift_y_px += -dy
            self.last_frame_action = "kept_dynamic"
        else:
            # Fixed-reference mode: shift incoming frame onto fixed reference
            limit_frac = float(self.settings.get("drift_limit_pct", 25.0)) / 100.0
            if abs(dx) > limit_frac * w or abs(dy) > limit_frac * h:
                self.drift_limit_reached = True
                self.rejected_drift_limit += 1
                self.drift_x_px = -dx
                self.drift_y_px = -dy
                self.last_frame_action = "drift_limit_recentre"
                return self._render_current(f32)

            aligned = shift_image(f32, dx, dy)
            self.drift_x_px = -dx
            self.drift_y_px = -dy

            # Welford running mean + variance with soft sigma-clipping once >= 6 frames
            n_prev = self.frames_kept
            if n_prev >= 6 and self.m2 is not None:
                std = np.sqrt(np.maximum(self.m2 / float(n_prev - 1), 4.0))
                sigma_k = float(self.settings.get("sigma_clip", 2.8))
                diff = aligned - self.accumulator
                clipped = np.where(np.abs(diff) > sigma_k * std, self.accumulator, aligned)
            else:
                clipped = aligned

            self.frames_kept += 1
            delta = clipped - self.accumulator
            self.accumulator += delta / float(self.frames_kept)
            delta2 = clipped - self.accumulator
            if self.m2 is not None:
                self.m2 += delta * delta2
            self.last_frame_action = "kept_fixed_ref"

        return self._render_current(f32)

    def _render_current(self, fallback_f32: np.ndarray) -> np.ndarray:
        src = self.accumulator if self.accumulator is not None else fallback_f32
        disp = stretch_for_display(
            src,
            black_point=float(self.settings["black_point"]),
            white_point=float(self.settings["white_point"]),
            stretch_asinh=float(self.settings["stretch_asinh"]),
        )
        self.last_display_rgb = disp
        return disp

    def metrics_dict(self, exposure_sec: float = 0.0) -> dict[str, Any]:
        h, w = (self.accumulator.shape[:2] if self.accumulator is not None else (480, 640))
        eff_exp = round(self.frames_kept * exposure_sec, 2) if exposure_sec > 0 else 0.0
        return {
            "frames_captured": self.frames_captured,
            "frames_kept": self.frames_kept,
            "rejected_quality": self.rejected_quality,
            "rejected_registration": self.rejected_registration,
            "rejected_clouds": self.rejected_clouds,
            "rejected_drift_limit": self.rejected_drift_limit,
            "last_score": self.last_score,
            "last_metric": self.last_metric,
            "last_star_count": self.last_star_count,
            "last_fwhm": self.last_fwhm,
            "current_threshold": self.current_threshold,
            "last_corr_response": self.last_response,
            "last_frame_action": self.last_frame_action,
            "drift_x_px": round(self.drift_x_px, 1),
            "drift_y_px": round(self.drift_y_px, 1),
            "drift_pct": round(100.0 * max(abs(self.drift_x_px) / max(w, 1), abs(self.drift_y_px) / max(h, 1)), 1),
            "drift_limit_reached": self.drift_limit_reached,
            "effective_exposure_sec": eff_exp,
            "dark_ready": self.dark_frame is not None,
            "dark_frames": self.dark_count,
            "dark_collecting": self._dark_collecting,
        }


# ---------------------------------------------------------------------------
# V4L2 Control Wrapper (enumerates & reads back real controls from webcam)
# ---------------------------------------------------------------------------

INTERESTING_CTRLS = {
    "auto_exposure",
    "exposure_auto",
    "exposure_time_absolute",
    "exposure_absolute",
    "exposure_dynamic_framerate",
    "gain",
    "gain_automatic",
    "white_balance_automatic",
    "white_balance_temperature_auto",
    "white_balance_temperature",
    "focus_automatic_continuous",
    "focus_auto",
    "focus_absolute",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
}


def list_v4l2_controls(device: str) -> dict[str, dict[str, Any]]:
    """Query real UVC controls via `v4l2-ctl --list-ctrls`."""
    if not shutil.which("v4l2-ctl"):
        return {}
    try:
        res = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-ctrls"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if res.returncode != 0:
            return {}
    except Exception:
        return {}

    ctrls: dict[str, dict[str, Any]] = {}
    for line in res.stdout.splitlines():
        m = re.match(r"^\s*([a-z0-9_]+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$", line)
        if not m:
            continue
        name, ctype, rest = m.group(1), m.group(2), m.group(3)
        if name not in INTERESTING_CTRLS:
            continue
        entry: dict[str, Any] = {"type": ctype}
        for key in ("min", "max", "step", "default", "value"):
            km = re.search(rf"\b{key}=(-?\d+)", rest)
            if km:
                entry[key] = int(km.group(1))
        entry["inactive"] = "flags=inactive" in rest
        ctrls[name] = entry
    return ctrls


def set_v4l2_controls(device: str, updates: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Apply validated integer V4L2 controls and read back actual device values."""
    if not shutil.which("v4l2-ctl") or not updates:
        return list_v4l2_controls(device)
    avail = list_v4l2_controls(device)
    for name, val in updates.items():
        if name not in avail:
            continue
        meta = avail[name]
        ival = int(val)
        if "min" in meta:
            ival = max(meta["min"], ival)
        if "max" in meta:
            ival = min(meta["max"], ival)
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", device, f"--set-ctrl={name}={ival}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            continue
    return list_v4l2_controls(device)


def lock_auto_controls(device: str) -> dict[str, dict[str, Any]]:
    """Kill Logitech UVC auto exposure/gain/WB/focus and read back actual values."""
    avail = list_v4l2_controls(device)
    to_set: dict[str, int] = {}
    # UVC auto_exposure / exposure_auto: 1 = Manual Mode (3 = Aperture Priority)
    for k in ("auto_exposure", "exposure_auto"):
        if k in avail:
            to_set[k] = 1
    for k in (
        "exposure_dynamic_framerate",
        "gain_automatic",
        "white_balance_automatic",
        "white_balance_temperature_auto",
        "focus_automatic_continuous",
        "focus_auto",
    ):
        if k in avail:
            to_set[k] = 0
    return set_v4l2_controls(device, to_set)


def estimate_exposure_seconds(controls: dict[str, dict[str, Any]], measured_fps: float) -> float:
    """Estimate single-frame exposure time in seconds (V4L2 exposure_absolute is in 100 us units)."""
    for k in ("exposure_time_absolute", "exposure_absolute"):
        if k in controls and "value" in controls[k]:
            val = int(controls[k]["value"])
            if val > 0:
                return val * 0.0001
    return (1.0 / measured_fps) if measured_fps > 0.1 else 0.0


# ---------------------------------------------------------------------------
# Camera Daemon Loop
# ---------------------------------------------------------------------------

def resolve_camera_device() -> Optional[str]:
    cfg_dev = os.environ.get("CAMERA_DEVICE", "/dev/astrocam").strip()
    if cfg_dev and os.path.exists(cfg_dev):
        return os.path.realpath(cfg_dev)
    for cand in sorted(glob.glob("/dev/video*")):
        # Skip Raspberry Pi internal ISP/codec nodes (typically >= /dev/video10)
        m = re.match(r"^/dev/video(\d+)$", cand)
        if m and int(m.group(1)) < 10:
            return os.path.realpath(cand)
    return None


def publish_camera_status(payload: dict[str, Any]) -> None:
    payload["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        write_json_atomic(STATUS_FILE, payload)
    except OSError as exc:
        LOG.warning("Failed writing camera-status.json: %s", exc)


def write_preview_atomic(jpeg_bytes: bytes) -> None:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PREVIEW_FILE.with_suffix(".tmp")
    tmp.write_bytes(jpeg_bytes)
    os.replace(tmp, PREVIEW_FILE)


def run_daemon() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    running = True

    def _stop(_sig: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    enabled = os.environ.get("CAMERA_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        publish_camera_status({
            "enabled": False,
            "state": "disabled",
            "message": "Camera service is disabled (opt-in). Install with --with-camera or set CAMERA_ENABLED=true.",
        })
        LOG.info("CAMERA_ENABLED is not true; exiting cleanly.")
        return 0

    if cv2 is None:
        msg = "OpenCV (python3-opencv) is not installed. Run `sudo /opt/astrodrive-src/install.sh --with-camera`."
        LOG.error("%s", msg)
        publish_camera_status({"enabled": True, "state": "missing_dependency", "message": msg})
        return 1

    CMD_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    stacker = LiveStacker(read_json_file(CMD_CONFIG_FILE))
    cap = None
    active_dev: Optional[str] = None
    active_res = str(stacker.settings.get("resolution", "640x480"))
    controls: dict[str, dict[str, Any]] = {}
    last_preview_ts = 0.0
    last_status_ts = 0.0
    last_cfg_mtime = 0.0
    fps_window: collections.deque[float] = collections.deque(maxlen=20)
    last_capture_msg = ""

    def close_cap() -> None:
        nonlocal cap, active_dev
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
            cap = None
        active_dev = None

    def process_commands(dev: Optional[str]) -> None:
        nonlocal active_res, controls, last_cfg_mtime, last_capture_msg
        if CMD_CONFIG_FILE.is_file():
            try:
                mt = CMD_CONFIG_FILE.stat().st_mtime
                if mt > last_cfg_mtime:
                    last_cfg_mtime = mt
                    cfg = read_json_file(CMD_CONFIG_FILE)
                    v4l2_req = cfg.pop("v4l2_controls", None)
                    stacker.update_settings(cfg)
                    new_res = str(stacker.settings.get("resolution", "640x480"))
                    if new_res in SUPPORTED_RESOLUTIONS and new_res != active_res:
                        active_res = new_res
                        close_cap()
                        stacker.reset()
                    if dev and isinstance(v4l2_req, dict) and v4l2_req:
                        controls = set_v4l2_controls(dev, v4l2_req)
            except Exception as exc:
                LOG.warning("Error reading camera config.json: %s", exc)

        for action_file in sorted(CMD_DIR.glob("action-*.json")):
            try:
                act = json.loads(action_file.read_text(encoding="utf-8"))
                action_file.unlink(missing_ok=True)
            except Exception:
                action_file.unlink(missing_ok=True)
                continue
            kind = str(act.get("action", ""))
            if kind == "stack_reset":
                stacker.reset()
                LOG.info("Stack reset requested.")
            elif kind == "lock_auto" and dev:
                controls = lock_auto_controls(dev)
                LOG.info("Locked UVC auto controls on %s.", dev)
            elif kind == "capture_dark":
                stacker.start_dark_capture(int(act.get("count", 15)))
                LOG.info("Started dark frame capture.")
            elif kind == "clear_dark":
                stacker.clear_dark()
                LOG.info("Cleared master dark frame.")
            elif kind == "save":
                if stacker.last_display_rgb is not None:
                    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
                    png_path = CAPTURES_DIR / f"astrodrive-{ts}.png"
                    meta_path = CAPTURES_DIR / f"astrodrive-{ts}.json"
                    save_png(stacker.last_display_rgb, png_path)
                    exp_s = estimate_exposure_seconds(controls, _fps(fps_window))
                    write_json_atomic(meta_path, {
                        "file": png_path.name,
                        "saved_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "device": dev,
                        "resolution": active_res,
                        "settings": stacker.settings,
                        "metrics": stacker.metrics_dict(exp_s),
                        "v4l2_controls": controls,
                    })
                    last_capture_msg = f"Saved {png_path.name}"
                    LOG.info("Saved stacked image to %s", png_path)

    def _fps(win: collections.deque[float]) -> float:
        if len(win) < 2:
            return 0.0
        dt_s = win[-1] - win[0]
        return round((len(win) - 1) / dt_s, 2) if dt_s > 0.01 else 0.0

    LOG.info("AstroDrive camera daemon starting (baseline: Pi 3B, default %s YUYV).", active_res)
    try:
        while running:
            dev = resolve_camera_device()
            process_commands(dev)

            if not dev:
                close_cap()
                publish_camera_status({
                    "enabled": True,
                    "state": "disconnected",
                    "device": None,
                    "message": "No USB camera detected (/dev/astrocam or /dev/video0). Plug in your Logitech webcam.",
                    "settings": stacker.settings,
                    "metrics": stacker.metrics_dict(0.0),
                    "controls": {},
                    "last_capture": last_capture_msg,
                })
                time.sleep(2.5)
                continue

            # Arbitrate device access cleanly with INDI (never crash-loop if indi_v4l2_ccd holds it)
            if cap is None:
                holders = find_device_holders(dev)
                if holders:
                    indi_busy = any("indi" in c for _, c in holders)
                    publish_camera_status({
                        "enabled": True,
                        "state": "busy_indi" if indi_busy else "busy",
                        "device": dev,
                        "holders": [{"pid": p, "comm": c} for p, c in holders],
                        "message": (
                            f"Camera {dev} is currently held by INDI ({holders[0][1]}). Disconnect the INDI V4L2 camera driver to use AstroDrive live view."
                            if indi_busy
                            else f"Camera {dev} is in use by {holders[0][1]} (PID {holders[0][0]})."
                        ),
                        "settings": stacker.settings,
                        "metrics": stacker.metrics_dict(0.0),
                        "controls": controls,
                        "last_capture": last_capture_msg,
                    })
                    time.sleep(3.0)
                    continue

                w, h = SUPPORTED_RESOLUTIONS.get(active_res, (640, 480))
                new_cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
                if not new_cap.isOpened():
                    publish_camera_status({
                        "enabled": True,
                        "state": "error",
                        "device": dev,
                        "message": f"Unable to open {dev} via V4L2.",
                        "settings": stacker.settings,
                        "metrics": stacker.metrics_dict(0.0),
                        "controls": controls,
                        "last_capture": last_capture_msg,
                    })
                    time.sleep(3.0)
                    continue

                # Prefer YUYV (uncompressed) so MJPEG block noise doesn't poison sharpness grading
                new_cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
                new_cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                new_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                new_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap = new_cap
                active_dev = dev
                controls = list_v4l2_controls(dev)
                LOG.info("Opened %s at %dx%d (YUYV preferred).", dev, w, h)

            ok, bgr = cap.read()
            if not ok or bgr is None:
                LOG.warning("Frame read failed on %s; reopening.", active_dev)
                close_cap()
                time.sleep(1.0)
                continue

            now = time.monotonic()
            fps_window.append(now)
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            disp_rgb = stacker.process_frame(rgb)

            # Throttle JPEG preview encoding to <= 2 fps to save Pi 3B CPU (tmpfs RAM only!)
            if now - last_preview_ts >= 0.5:
                try:
                    write_preview_atomic(encode_jpeg(disp_rgb, quality=80))
                    last_preview_ts = now
                except Exception as exc:
                    LOG.warning("Preview JPEG encode error: %s", exc)

            if now - last_status_ts >= 1.5:
                fps_val = _fps(fps_window)
                exp_s = estimate_exposure_seconds(controls, fps_val)
                publish_camera_status({
                    "enabled": True,
                    "state": "stacking" if stacker.settings.get("stacking") else "preview",
                    "device": active_dev,
                    "resolution": f"{bgr.shape[1]}x{bgr.shape[0]}",
                    "fps": fps_val,
                    "settings": stacker.settings,
                    "metrics": stacker.metrics_dict(exp_s),
                    "controls": controls,
                    "preview_ready": PREVIEW_FILE.is_file(),
                    "last_capture": last_capture_msg,
                    "message": "Live stacking active" if stacker.settings.get("stacking") else "Live preview active",
                })
                last_status_ts = now
    finally:
        close_cap()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_daemon())
