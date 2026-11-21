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
CMD_DIR = STATE_DIR / "camera-cmd"
CMD_CONFIG_FILE = CMD_DIR / "config.json"
CAPTURES_DIR = STATE_DIR / "captures"
PREVIEW_DIR = Path(os.environ.get("CAMERA_PREVIEW_DIR", "/run/onstep-camera"))
PREVIEW_FILE = PREVIEW_DIR / "preview.jpg"
# Status JSON lives in tmpfs (/run/onstep-camera) for zero periodic SD card writes
STATUS_FILE = Path(os.environ.get("CAMERA_STATUS_FILE", str(PREVIEW_DIR / "camera-status.json")))

SUPPORTED_RESOLUTIONS = {
    "320x240": (320, 240),
    "640x480": (640, 480),
    "800x600": (800, 600),
    "1280x720": (1280, 720),
}
VALID_MODES = {"dynamic_drift", "fixed_ref"}

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
    "quality_floor": 0.5,         # absolute minimum sharpness score
    "max_fps": 8.0,               # Pi 3B CPU budget cap (leaves headroom for INDI)
}

_HANN_CACHE: dict[tuple[int, int, bool], np.ndarray] = {}


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
    """Consistent 3x3 binomial/Gaussian blur on both OpenCV and pure-NumPy paths."""
    if cv2 is not None:
        return cv2.GaussianBlur(img, (3, 3), 0.85)
    # Separable 1-2-1 3x3 blur in pure numpy
    p = np.pad(img, 1, mode="edge")
    h = 0.25 * p[:, :-2] + 0.5 * p[:, 1:-1] + 0.25 * p[:, 2:]
    return 0.25 * h[:-2, :] + 0.5 * h[1:-1, :] + 0.25 * h[2:, :]


def _detect_stars_and_fwhm(mono: np.ndarray) -> tuple[int, int, float]:
    """
    Fast lightweight star detector on binned mono image.
    Returns (raw_peak_count, valid_fwhm_count, median_fwhm_px) so severely blurred
    stars still register as peaks and do not falsely trigger cloud lockout (R9).
    """
    med = float(np.median(mono))
    mad = float(np.median(np.abs(mono - med)))
    sigma = max(1.4826 * mad, 1.5)
    thresh = med + 6.0 * sigma
    mask = mono > thresh
    if not np.any(mask):
        return 0, 0, 0.0

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
        return 0, 0, 0.0

    # Keep top 40 brightest peaks away from edges
    h, w = mono.shape
    vals = mono[ys, xs]
    order = np.argsort(vals)[::-1][:40]
    raw_peaks = 0
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
        # Ignore extended bright discs/limbs (Moon/planet plateaus) where >=25% of patch border is nearly as bright as center
        border = np.concatenate([patch[0, :], patch[-1, :], patch[1:-1, 0], patch[1:-1, -1]])
        if float(np.percentile(border, 75)) > 0.70 * peak:
            continue
        raw_peaks += 1
        yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
        cy = float((yy * patch).sum() / total)
        cx = float((xx * patch).sum() / total)
        var = float((((yy - cy) ** 2 + (xx - cx) ** 2) * patch).sum() / total)
        sigma_star = math.sqrt(max(var * 0.5, 0.15))
        fwhm = 2.355 * sigma_star
        if 1.0 <= fwhm <= 12.0:
            fwhms.append(fwhm)

    if not fwhms:
        return raw_peaks, 0, 0.0
    return raw_peaks, len(fwhms), float(np.median(fwhms))


def grade_frame(mono: np.ndarray) -> dict[str, Any]:
    """
    Grade sharpness on a binned mono frame:
      1. Star-based (Siril model): if >= 3 valid-FWHM stars found, score = 100 / median_FWHM.
      2. Blurred-Laplacian fallback (PSS model): Gaussian blur first so Logitech
         high-gain sensor noise does not inflate sharpness score.
    """
    raw_peaks, star_count, median_fwhm = _detect_stars_and_fwhm(mono)
    if star_count >= 3 and median_fwhm > 0.0:
        return {
            "metric": "star_fwhm",
            "score": round(100.0 / median_fwhm, 3),
            "star_count": star_count,
            "raw_peak_count": raw_peaks,
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
        "raw_peak_count": raw_peaks,
        "median_fwhm": round(median_fwhm, 2),
    }


def _get_hanning_window(h: int, w: int, use_cv2: bool) -> np.ndarray:
    key = (h, w, use_cv2)
    win = _HANN_CACHE.get(key)
    if win is None:
        if len(_HANN_CACHE) > 8:
            _HANN_CACHE.clear()
        if use_cv2 and cv2 is not None:
            win = cv2.createHanningWindow((w, h), cv2.CV_32F)
        else:
            wy = np.hanning(h).astype(np.float32)[:, None]
            wx = np.hanning(w).astype(np.float32)[None, :]
            win = wy * wx
        _HANN_CACHE[key] = win
    return win


def phase_correlate(ref_mono: np.ndarray, cur_mono: np.ndarray) -> tuple[float, float, float]:
    """Return (dx, dy, response) where shifting cur by (+dx, +dy) aligns it to ref."""
    h, w = ref_mono.shape
    if cv2 is not None:
        hann = _get_hanning_window(h, w, True)
        (shift_x, shift_y), response = cv2.phaseCorrelate(cur_mono, ref_mono, hann)
        return float(shift_x), float(shift_y), float(response)

    # Pure-NumPy FFT phase correlation fallback with 3-point parabolic sub-pixel refinement
    win = _get_hanning_window(h, w, False)
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

    # 3-point parabolic interpolation around peak (mod h/w)
    def _subpix(v_minus: float, v_zero: float, v_plus: float) -> float:
        denom = 2.0 * (v_minus - 2.0 * v_zero + v_plus)
        if abs(denom) < 1e-9:
            return 0.0
        return float(np.clip((v_minus - v_plus) / denom, -0.5, 0.5))

    sub_x = _subpix(float(corr[py, (px - 1) % w]), response, float(corr[py, (px + 1) % w]))
    sub_y = _subpix(float(corr[(py - 1) % h, px]), response, float(corr[(py + 1) % h, px]))

    dy = float(py if py <= h // 2 else py - h) + sub_y
    dx = float(px if px <= w // 2 else px - w) + sub_x
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
      - Per-frame lucky-imaging grading (star FWHM + cloud recovery OR PSS blurred-Laplacian)
      - Separate rolling 200-frame score histories per metric (R10)
      - Continuous drift auto-alignment (`dynamic_drift` OLS decay OR `fixed_ref` float64 Winsorised sigma-clip)
      - Optional master dark-frame capture & subtraction
      - On-demand asinh display stretch to keep Pi 3B CPU lean
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

        # R10: Separate rolling score histories per metric so FWHM and Laplacian scales never mix
        self.score_history_star: collections.deque[float] = collections.deque(maxlen=200)
        self.score_history_lap: collections.deque[float] = collections.deque(maxlen=200)
        self.star_history: collections.deque[int] = collections.deque(maxlen=60)
        self.consecutive_cloud_rejections: int = 0
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
        """Strictly validate & coerce settings so malformed config.json never crashes or freezes stacking."""
        if not isinstance(updates, dict):
            return
        old_mode = self.settings.get("mode")

        def _f(val: Any, lo: float, hi: float, fallback: float) -> float:
            try:
                v = float(val)
                if math.isnan(v) or math.isinf(v):
                    return fallback
                return max(lo, min(hi, v))
            except (TypeError, ValueError):
                return fallback

        def _i(val: Any, lo: int, hi: int, fallback: int) -> int:
            try:
                return max(lo, min(hi, int(val)))
            except (TypeError, ValueError):
                return fallback

        if "stacking" in updates and isinstance(updates["stacking"], bool):
            self.settings["stacking"] = updates["stacking"]
        if "mode" in updates and str(updates["mode"]).strip() in VALID_MODES:
            self.settings["mode"] = str(updates["mode"]).strip()
        if "keep_pct" in updates:
            self.settings["keep_pct"] = _i(updates["keep_pct"], 10, 100, int(self.settings["keep_pct"]))
        if "resolution" in updates and str(updates["resolution"]).strip() in SUPPORTED_RESOLUTIONS:
            self.settings["resolution"] = str(updates["resolution"]).strip()
        if "stretch_asinh" in updates:
            self.settings["stretch_asinh"] = _f(updates["stretch_asinh"], 0.0, 20.0, float(self.settings["stretch_asinh"]))
        if "black_point" in updates:
            self.settings["black_point"] = _f(updates["black_point"], 0.0, 0.8, float(self.settings["black_point"]))
        if "white_point" in updates:
            self.settings["white_point"] = _f(updates["white_point"], 0.1, 1.0, float(self.settings["white_point"]))
        if float(self.settings["white_point"]) <= float(self.settings["black_point"]) + 0.05:
            self.settings["white_point"] = min(1.0, float(self.settings["black_point"]) + 0.06)
        if "sigma_clip" in updates:
            self.settings["sigma_clip"] = _f(updates["sigma_clip"], 0.5, 10.0, float(self.settings["sigma_clip"]))
        if "decay_frames" in updates:
            self.settings["decay_frames"] = _i(updates["decay_frames"], 4, 200, int(self.settings["decay_frames"]))
        if "drift_limit_pct" in updates:
            self.settings["drift_limit_pct"] = _f(updates["drift_limit_pct"], 5.0, 60.0, float(self.settings["drift_limit_pct"]))
        if "min_corr_response" in updates:
            self.settings["min_corr_response"] = _f(updates["min_corr_response"], 0.01, 0.9, float(self.settings["min_corr_response"]))
        if "settle_skip_frames" in updates:
            self.settings["settle_skip_frames"] = _i(updates["settle_skip_frames"], 0, 20, int(self.settings["settle_skip_frames"]))
        if "quality_floor" in updates:
            self.settings["quality_floor"] = _f(updates["quality_floor"], 0.0, 100.0, float(self.settings["quality_floor"]))
        if "max_fps" in updates:
            self.settings["max_fps"] = _f(updates["max_fps"], 0.5, 30.0, float(self.settings["max_fps"]))

        if self.settings.get("mode") != old_mode:
            self.reset()

    def reset(self) -> None:
        self.accumulator = None
        self.m2 = None
        self.ref_align_mono = None
        self.score_history_star.clear()
        self.score_history_lap.clear()
        self.star_history.clear()
        self.consecutive_cloud_rejections = 0
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

    def _compute_threshold(self, metric: str) -> float:
        """Compute rolling keep-% threshold ONLY over frames sharing the same metric (R10)."""
        keep_pct = float(np.clip(self.settings.get("keep_pct", 50), 10, 100))
        floor = float(self.settings.get("quality_floor", 0.5))
        hist = self.score_history_star if metric == "star_fwhm" else self.score_history_lap
        if len(hist) < 8 or keep_pct >= 99.9:
            return floor
        percentile = 100.0 - keep_pct
        dyn = float(np.percentile(list(hist), percentile))
        return max(floor, dyn)

    def render_display(self, fallback_f32: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
        """Stretch current stack (or fallback frame) on demand for preview/save."""
        src = self.accumulator if self.accumulator is not None else fallback_f32
        if src is None:
            return self.last_display_rgb
        disp = stretch_for_display(
            src,
            black_point=float(self.settings["black_point"]),
            white_point=float(self.settings["white_point"]),
            stretch_asinh=float(self.settings["stretch_asinh"]),
        )
        self.last_display_rgb = disp
        return disp

    def process_frame(self, frame_rgb_u8: np.ndarray, render_preview: bool = True) -> Optional[np.ndarray]:
        """
        Process one RGB uint8 frame through the full EAA pipeline.
        When `render_preview=False`, skips the full-frame asinh stretch to save Pi 3B CPU.
        """
        f32 = frame_rgb_u8.astype(np.float32)

        # Master dark collection mode (camera capped)
        if self._dark_collecting:
            if self._dark_sum is None or self._dark_sum.shape != f32.shape:
                self._dark_sum = np.zeros_like(f32, dtype=np.float64)
                self._dark_collected = 0
            self._dark_sum += f32
            self._dark_collected += 1
            self.last_frame_action = f"capturing_dark ({self._dark_collected}/{self._dark_target})"
            if self._dark_collected >= self._dark_target:
                self.dark_frame = (self._dark_sum / float(self._dark_collected)).astype(np.float32)
                self.dark_count = self._dark_collected
                self._dark_collecting = False
                self._dark_sum = None
                self.last_frame_action = f"dark_ready ({self.dark_count} frames)"
            self.last_display_rgb = frame_rgb_u8
            return frame_rgb_u8 if render_preview else None

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
            if render_preview:
                disp = stretch_for_display(
                    f32,
                    black_point=float(self.settings["black_point"]),
                    white_point=float(self.settings["white_point"]),
                    stretch_asinh=float(self.settings["stretch_asinh"]),
                )
                self.last_display_rgb = disp
                return disp
            return None

        # Settle-skip period after sudden mount bump/nudge
        if self.settle_skip_remaining > 0:
            self.settle_skip_remaining -= 1
            self.last_frame_action = f"settling ({self.settle_skip_remaining} left)"
            return self.render_display(f32) if render_preview else None

        # 1. Score BEFORE registration (on binned mono copy)
        grade_mono, _ = to_binned_mono(f32, max_dim=320)
        g = grade_frame(grade_mono)
        self.last_score = float(g["score"])
        self.last_metric = str(g["metric"])
        self.last_star_count = int(g["star_count"])
        raw_peaks = int(g.get("raw_peak_count", self.last_star_count))
        self.last_fwhm = float(g["median_fwhm"])

        # R9: Cloud dropout check using raw peak count (so blurred stars don't spoof clouds).
        # Always append observed counts and auto-clear after 15 consecutive cloud frames so
        # slewing to a genuinely sparse field NEVER permanently locks out stacking.
        if len(self.star_history) >= 5:
            med_stars = float(np.median(list(self.star_history)))
            if med_stars >= 4.0 and raw_peaks < max(2, int(0.30 * med_stars)):
                self.star_history.append(raw_peaks)
                self.consecutive_cloud_rejections += 1
                if self.consecutive_cloud_rejections >= 15:
                    # User likely slewed to a sparser field or extended target -> adapt baseline
                    self.star_history.clear()
                    self.consecutive_cloud_rejections = 0
                    self.last_frame_action = "cloud_baseline_reset"
                else:
                    self.rejected_clouds += 1
                    self.last_frame_action = "rejected_clouds"
                    return self.render_display(f32) if render_preview else None
            else:
                self.consecutive_cloud_rejections = 0
                if raw_peaks >= 3:
                    self.star_history.append(raw_peaks)
        elif raw_peaks >= 3:
            self.star_history.append(raw_peaks)

        # R10: Append score to metric-specific history
        hist = self.score_history_star if self.last_metric == "star_fwhm" else self.score_history_lap
        hist.append(self.last_score)
        self.current_threshold = round(self._compute_threshold(self.last_metric), 3)

        # Exempt initial reference acquisition from quality_floor so low-texture targets can start stacking
        if self.accumulator is not None and self.last_score < self.current_threshold:
            self.rejected_quality += 1
            self.last_frame_action = "rejected_quality"
            return self.render_display(f32) if render_preview else None

        # 2. Align on aggressively binned mono copy (<=320px, <=240px for 720p)
        align_mono, align_scale = to_binned_mono(f32, max_dim=240 if f32.shape[1] >= 1000 else 320)
        mode = str(self.settings.get("mode", "dynamic_drift"))
        h, w = f32.shape[:2]

        if self.accumulator is None or self.ref_align_mono is None:
            # float64 accumulator & m2 (~7.4 MB @ 640x480x3) eliminates float32 ULP freeze on long stacks
            self.accumulator = f32.astype(np.float64)
            self.m2 = np.zeros_like(self.accumulator, dtype=np.float64)
            self.ref_align_mono = align_mono
            self.align_scale = align_scale
            self.frames_kept = 1
            self.drift_x_px = 0.0
            self.drift_y_px = 0.0
            self.drift_limit_reached = False
            self.last_response = 1.0
            self.last_frame_action = "reference_acquired"
            return self.render_display(f32) if render_preview else None

        dx_b, dy_b, response = phase_correlate(self.ref_align_mono, align_mono)
        dx = dx_b * align_scale
        dy = dy_b * align_scale
        self.last_response = round(response, 3)

        min_resp = float(self.settings.get("min_corr_response", 0.08))
        if response < min_resp:
            self.rejected_registration += 1
            self.last_frame_action = "rejected_registration"
            return self.render_display(f32) if render_preview else None

        if mode == "dynamic_drift":
            # OpenLiveStacker v34 non-tracking mode:
            # Anchor stack to the *latest* live position by shifting the accumulator
            # by (-dx, -dy) and blending the fresh frame with exponential decay.
            jump_limit = 0.15 * min(h, w)
            if abs(dx) > jump_limit or abs(dy) > jump_limit:
                self.reset()
                self.settle_skip_remaining = int(self.settings.get("settle_skip_frames", 3))
                self.last_frame_action = "bump_reset_settling"
                return self.render_display(f32) if render_preview else None

            shifted_acc = shift_image(self.accumulator.astype(np.float32), -dx, -dy).astype(np.float64)
            self.frames_kept += 1
            decay_n = max(4, int(self.settings.get("decay_frames", 24)))
            alpha = 1.0 / float(min(self.frames_kept, decay_n))
            self.accumulator = (1.0 - alpha) * shifted_acc + alpha * f32.astype(np.float64)
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
                return self.render_display(f32) if render_preview else None

            self.drift_limit_reached = False
            aligned = shift_image(f32, dx, dy).astype(np.float64)
            self.drift_x_px = -dx
            self.drift_y_px = -dy

            # Float64 Welford running mean + Winsorised variance update (prevents variance-collapse feedback loop)
            n_prev = self.frames_kept
            if n_prev >= 6 and self.m2 is not None:
                std = np.sqrt(np.maximum(self.m2 / float(n_prev - 1), 4.0))
                sigma_k = float(self.settings.get("sigma_clip", 2.8))
                diff = aligned - self.accumulator
                bound = sigma_k * std
                winsorised = self.accumulator + np.clip(diff, -bound, bound)
            else:
                winsorised = aligned

            self.frames_kept += 1
            delta = winsorised - self.accumulator
            self.accumulator += delta / float(self.frames_kept)
            delta2 = winsorised - self.accumulator
            if self.m2 is not None:
                self.m2 += delta * delta2
            self.last_frame_action = "kept_fixed_ref"

        return self.render_display(f32) if render_preview else None

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
            "drift_mode_meaning": "cumulative" if self.settings.get("mode") == "dynamic_drift" else "offset_from_ref",
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

def _decode_fourcc(fourcc_float: float) -> str:
    val = int(fourcc_float)
    chars = [chr((val >> (8 * i)) & 0xFF) for i in range(4)]
    s = "".join(c if 32 <= ord(c) < 127 else "?" for c in chars)
    return s.strip() or "UNKNOWN"


def resolve_camera_device() -> tuple[Optional[str], bool]:
    """Return (device_path, used_fallback)."""
    cfg_dev = os.environ.get("CAMERA_DEVICE", "/dev/astrocam").strip()
    if cfg_dev and os.path.exists(cfg_dev):
        return os.path.realpath(cfg_dev), False
    for cand in sorted(glob.glob("/dev/video*")):
        # Skip Raspberry Pi internal ISP/codec nodes (typically >= /dev/video10)
        m = re.match(r"^/dev/video(\d+)$", cand)
        if m and int(m.group(1)) < 10:
            return os.path.realpath(cand), True
    return None, False


def publish_camera_status(payload: dict[str, Any]) -> None:
    payload["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        write_json_atomic(STATUS_FILE, payload)
    except OSError as exc:
        LOG.warning("Failed writing camera-status.json: %s", exc)


def write_preview_atomic(jpeg_bytes: bytes) -> None:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PREVIEW_DIR / f".{PREVIEW_FILE.name}.{os.getpid()}.tmp"
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
            "message": "Camera service is disabled. Toggle Enable Camera Service in Dashboard or set CAMERA_ENABLED=true.",
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
    active_fourcc = "UNKNOWN"
    controls: dict[str, dict[str, Any]] = {}
    last_preview_ts = 0.0
    last_status_ts = 0.0
    last_frame_ts = 0.0
    last_cfg_sig: tuple[int, int] = (0, 0)
    fps_window: collections.deque[float] = collections.deque(maxlen=20)
    last_capture_msg = ""
    logged_fallback_dev: Optional[str] = None

    def close_cap() -> None:
        nonlocal cap, active_dev, active_fourcc
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
            cap = None
        active_dev = None
        active_fourcc = "UNKNOWN"

    def _fps(win: collections.deque[float]) -> float:
        if len(win) < 2:
            return 0.0
        dt_s = win[-1] - win[0]
        return round((len(win) - 1) / dt_s, 2) if dt_s > 0.01 else 0.0

    def process_commands(dev: Optional[str]) -> None:
        nonlocal active_res, controls, last_cfg_sig, last_capture_msg
        if CMD_CONFIG_FILE.is_file():
            try:
                st = CMD_CONFIG_FILE.stat()
                sig = (st.st_mtime_ns, st.st_size)
                if sig != last_cfg_sig:
                    last_cfg_sig = sig
                    cfg = read_json_file(CMD_CONFIG_FILE)
                    v4l2_req = cfg.pop("v4l2_controls", None)
                    stacker.update_settings(cfg)
                    new_res = str(stacker.settings.get("resolution", "640x480"))
                    if new_res in SUPPORTED_RESOLUTIONS and new_res != active_res:
                        active_res = new_res
                        close_cap()
                        stacker.reset()
                    # R12: Never write V4L2 controls while a foreign holder (e.g. INDI) owns the webcam
                    if dev and isinstance(v4l2_req, dict) and v4l2_req:
                        if find_device_holders(dev):
                            LOG.info("Skipping V4L2 control update while another process holds %s.", dev)
                        else:
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
            try:
                kind = str(act.get("action", ""))
                if kind == "stack_reset":
                    stacker.reset()
                    LOG.info("Stack reset requested.")
                elif kind == "lock_auto" and dev:
                    if find_device_holders(dev):
                        LOG.info("Skipping lock_auto while another process holds %s.", dev)
                    else:
                        controls = lock_auto_controls(dev)
                        LOG.info("Locked UVC auto controls on %s.", dev)
                elif kind == "capture_dark":
                    stacker.start_dark_capture(int(act.get("count", 15)))
                    LOG.info("Started dark frame capture.")
                elif kind == "clear_dark":
                    stacker.clear_dark()
                    LOG.info("Cleared master dark frame.")
                elif kind == "save":
                    disp = stacker.render_display()
                    if disp is not None:
                        now_utc = dt.datetime.now(dt.timezone.utc)
                        ms = now_utc.microsecond // 1000
                        ts = f"{now_utc.strftime('%Y%m%d-%H%M%S')}-{ms:03d}"
                        png_path = CAPTURES_DIR / f"astrodrive-{ts}.png"
                        meta_path = CAPTURES_DIR / f"astrodrive-{ts}.json"
                        save_png(disp, png_path)
                        exp_s = estimate_exposure_seconds(controls, _fps(fps_window))
                        write_json_atomic(meta_path, {
                            "file": png_path.name,
                            "saved_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "device": dev,
                            "resolution": active_res,
                            "pixel_format": active_fourcc,
                            "settings": stacker.settings,
                            "metrics": stacker.metrics_dict(exp_s),
                            "v4l2_controls": controls,
                        })
                        last_capture_msg = f"Saved {png_path.name}"
                        LOG.info("Saved stacked image to %s", png_path)
            except Exception as exc:
                # R11: Never let a failed save/action crash the daemon and lose the in-RAM stack
                last_capture_msg = f"Action error: {exc}"
                LOG.warning("Camera action %r failed cleanly (stack preserved): %s", act.get("action"), exc)

    def _publish_busy(dev: str, holders: list[tuple[int, str]]) -> None:
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

    LOG.info("AstroDrive camera daemon starting (baseline: Pi 3B, default %s YUYV, status in %s).", active_res, STATUS_FILE)
    try:
        while running:
            try:
                dev, used_fallback = resolve_camera_device()
                if dev and used_fallback and dev != logged_fallback_dev:
                    LOG.info("/dev/astrocam not present; falling back to V4L2 node %s.", dev)
                    logged_fallback_dev = dev
                elif not used_fallback:
                    logged_fallback_dev = None

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

                # Arbitrate device access cleanly with INDI before opening
                if cap is None:
                    holders = find_device_holders(dev)
                    if holders:
                        _publish_busy(dev, holders)
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

                    # Read back negotiated format & dimensions
                    actual_fourcc = _decode_fourcc(new_cap.get(cv2.CAP_PROP_FOURCC))
                    actual_w = int(new_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    actual_h = int(new_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    if actual_fourcc.upper() != "YUYV":
                        LOG.warning(
                            "Requested YUYV on %s, but driver negotiated %s (%dx%d). Sharpness grading may see compression artefacts.",
                            dev,
                            actual_fourcc,
                            actual_w,
                            actual_h,
                        )
                    else:
                        LOG.info("Opened %s at %dx%d (%s).", dev, actual_w, actual_h, actual_fourcc)

                    cap = new_cap
                    active_dev = dev
                    active_fourcc = actual_fourcc
                    controls = list_v4l2_controls(dev)

                # Pi 3B CPU rate cap (default 8 fps) so planetary rates never starve INDI
                max_fps = max(0.5, float(stacker.settings.get("max_fps", 8.0)))
                min_interval = 1.0 / max_fps
                now = time.monotonic()
                if last_frame_ts > 0.0 and (now - last_frame_ts) < min_interval:
                    time.sleep(min(0.05, min_interval - (now - last_frame_ts)))
                    continue

                ok, bgr = cap.read()
                if not ok or bgr is None:
                    LOG.warning("Frame read failed on %s; reopening (preserving stack).", active_dev)
                    close_cap()
                    time.sleep(1.0)
                    continue

                now = time.monotonic()
                last_frame_ts = now
                fps_window.append(now)
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

                # Only run full-frame asinh display stretch when a JPEG preview is actually due
                need_preview = (now - last_preview_ts >= 0.5) or (stacker.last_display_rgb is None)
                disp_rgb = stacker.process_frame(rgb, render_preview=need_preview)

                if need_preview and disp_rgb is not None:
                    try:
                        write_preview_atomic(encode_jpeg(disp_rgb, quality=80))
                        last_preview_ts = now
                    except Exception as exc:
                        LOG.warning("Preview JPEG encode error: %s", exc)

                if now - last_status_ts >= 1.5:
                    # R12: Re-check foreign device holders mid-stream so INDI opening the webcam
                    # causes us to release it immediately instead of fighting over V4L2.
                    if active_dev:
                        holders = find_device_holders(active_dev)
                        if holders:
                            LOG.info("Foreign process %s opened %s mid-stream; releasing camera cleanly.", holders, active_dev)
                            busy_dev = active_dev
                            close_cap()
                            _publish_busy(busy_dev, holders)
                            last_status_ts = now
                            time.sleep(2.5)
                            continue

                    fps_val = _fps(fps_window)
                    exp_s = estimate_exposure_seconds(controls, fps_val)
                    publish_camera_status({
                        "enabled": True,
                        "state": "stacking" if stacker.settings.get("stacking") else "preview",
                        "device": active_dev,
                        "resolution": f"{bgr.shape[1]}x{bgr.shape[0]}",
                        "pixel_format": active_fourcc,
                        "fps": fps_val,
                        "settings": stacker.settings,
                        "metrics": stacker.metrics_dict(exp_s),
                        "controls": controls,
                        "preview_ready": PREVIEW_FILE.is_file(),
                        "last_capture": last_capture_msg,
                        "message": "Live stacking active" if stacker.settings.get("stacking") else "Live preview active",
                    })
                    last_status_ts = now
            except Exception as exc:
                # R11: Never let a transient cv2/numpy/IO error kill the daemon & discard the in-RAM stack
                LOG.exception("Transient error in camera loop (preserving in-RAM stack): %s", exc)
                close_cap()
                time.sleep(1.5)
    finally:
        close_cap()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_daemon())
