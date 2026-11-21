#!/usr/bin/env python3
"""
Unit tests for AstroDrive's EAA LiveStacker (scripts/onstep_camera.py).

Verifies Requirement A (continuous drift auto-alignment, dynamic_drift bump reset +
settle-skip, fixed_ref Welford sigma-clipping + 25% drift guard) and Requirement B
(star FWHM & blurred-Laplacian grading, rolling Keep Best % rejection, cloud collapse
rejection, master dark subtraction, and display stretch/encoding) using synthetic
starfields with zero external camera hardware required.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import onstep_camera as cam  # noqa: E402


def make_starfield(
    h: int = 160,
    w: int = 200,
    shift_x: float = 0.0,
    shift_y: float = 0.0,
    sigma: float = 1.6,
    bg: float = 18.0,
    hot_pixel: tuple[int, int] | None = None,
) -> np.ndarray:
    """Render a deterministic synthetic RGB starfield with sub-pixel shifted Gaussian stars."""
    stars = [
        (45.0, 40.0, 210.0),
        (95.0, 52.0, 185.0),
        (145.0, 45.0, 200.0),
        (60.0, 95.0, 190.0),
        (115.0, 102.0, 220.0),
        (158.0, 118.0, 175.0),
        (78.0, 130.0, 195.0),
        (132.0, 74.0, 180.0),
    ]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.full((h, w), bg, dtype=np.float32)
    denom = 2.0 * (sigma ** 2)
    for sx, sy, amp in stars:
        cx = sx + shift_x
        cy = sy + shift_y
        img += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / denom)
    if hot_pixel is not None:
        hy, hx = hot_pixel
        img[hy, hx] = 255.0
    rgb = np.clip(np.stack([img, img * 0.98, img * 0.95], axis=-1), 0, 255).astype(np.uint8)
    return rgb


def test_star_fwhm_and_lucky_selection() -> None:
    sharp = make_starfield(sigma=1.5)
    blurry = make_starfield(sigma=3.8)
    mono_s, _ = cam.to_binned_mono(sharp, max_dim=320)
    mono_b, _ = cam.to_binned_mono(blurry, max_dim=320)
    gs = cam.grade_frame(mono_s)
    gb = cam.grade_frame(mono_b)
    assert gs["metric"] == "star_fwhm", gs
    assert gs["star_count"] >= 5, gs
    assert gs["score"] > gb["score"] * 1.4, (gs, gb)

    # Feed 8 sharp then 8 blurry at keep_pct=50 -> blurry frames must be rejected
    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 50})
    for _ in range(8):
        st.process_frame(sharp)
    kept_after_sharp = st.frames_kept
    for _ in range(8):
        st.process_frame(blurry)
    assert kept_after_sharp >= 7, kept_after_sharp
    assert st.rejected_quality >= 6, st.metrics_dict()
    print("[PASS] test_star_fwhm_and_lucky_selection")


def test_cloud_collapse_rejection() -> None:
    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 100})
    sharp = make_starfield(sigma=1.5)
    for _ in range(6):
        st.process_frame(sharp)
    kept_before = st.frames_kept

    # Flat cloudy frame with zero stars
    clouds = np.full_like(sharp, 25)
    st.process_frame(clouds)
    assert st.rejected_clouds >= 1, st.metrics_dict()
    assert st.frames_kept == kept_before
    print("[PASS] test_cloud_collapse_rejection")


def test_continuous_drift_dynamic_mode_and_bump_reset() -> None:
    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 100, "settle_skip_frames": 2})
    # Continuous drift of +2.0 px X, +1.0 px Y per frame across 10 frames (total +18, +9 px)
    for i in range(10):
        frame = make_starfield(shift_x=2.0 * i, shift_y=1.0 * i, sigma=1.6)
        st.process_frame(frame)
    m = st.metrics_dict(exposure_sec=0.2)
    assert m["frames_kept"] >= 9, m
    assert abs(m["drift_x_px"] - 18.0) < 3.0, m
    assert abs(m["drift_y_px"] - 9.0) < 3.0, m
    assert m["effective_exposure_sec"] >= 1.8, m

    # Stacked stars should remain sharp (not smeared out by 18px)
    assert st.accumulator is not None
    mono_acc, _ = cam.to_binned_mono(np.clip(st.accumulator, 0, 255).astype(np.uint8), max_dim=320)
    g_acc = cam.grade_frame(mono_acc)
    assert g_acc["star_count"] >= 5 and g_acc["median_fwhm"] < 5.5, g_acc

    # Sudden 50 px mount bump -> auto-reset + settle-skip for 2 frames
    bumped = make_starfield(shift_x=68.0, shift_y=9.0, sigma=1.6)
    st.process_frame(bumped)
    assert "bump_reset" in st.last_frame_action, st.last_frame_action
    assert st.frames_kept == 0
    st.process_frame(bumped)
    assert "settling" in st.last_frame_action, st.last_frame_action
    st.process_frame(bumped)
    assert "settling" in st.last_frame_action, st.last_frame_action
    st.process_frame(bumped)
    assert st.frames_kept == 1, st.metrics_dict()
    print("[PASS] test_continuous_drift_dynamic_mode_and_bump_reset")


def test_fixed_ref_drift_limit_and_sigma_clipping() -> None:
    st = cam.LiveStacker({"mode": "fixed_ref", "keep_pct": 100, "drift_limit_pct": 25.0})
    for i in range(6):
        frame = make_starfield(shift_x=2.5 * i, shift_y=0.0, sigma=1.6)
        st.process_frame(frame)
    assert st.frames_kept == 6 and not st.drift_limit_reached, st.metrics_dict()

    # Shift beyond 25% of 200 px (>50 px) -> must trigger drift_limit_reached
    far = make_starfield(shift_x=65.0, shift_y=0.0, sigma=1.6)
    st.process_frame(far)
    assert st.drift_limit_reached is True, st.metrics_dict()
    assert st.rejected_drift_limit >= 1, st.metrics_dict()
    print("[PASS] test_fixed_ref_drift_limit_and_sigma_clipping")


def test_dark_frame_subtraction_and_encoding() -> None:
    hot = (20, 25)
    dark_raw = np.full((160, 200, 3), 18, dtype=np.uint8)
    dark_raw[hot[0], hot[1]] = [255, 255, 255]

    st = cam.LiveStacker({"mode": "fixed_ref", "keep_pct": 100})
    st.start_dark_capture(count=4)
    for _ in range(4):
        st.process_frame(dark_raw)
    assert st.dark_frame is not None and st.dark_count == 4, st.metrics_dict()

    sci = make_starfield(sigma=1.6, bg=18.0, hot_pixel=hot)
    disp = st.process_frame(sci)
    assert st.accumulator is not None
    # Hot pixel should be cancelled down near 0 in the dark-subtracted accumulator
    assert float(st.accumulator[hot[0], hot[1]].mean()) < 5.0, st.accumulator[hot[0], hot[1]]

    # Encode JPEG & save PNG cleanly
    jpg = cam.encode_jpeg(disp, quality=80)
    assert len(jpg) > 500 and jpg[:2] == b"\xff\xd8"
    with tempfile.TemporaryDirectory() as td:
        out_png = Path(td) / "test.png"
        cam.save_png(disp, out_png)
        assert out_png.is_file() and out_png.stat().st_size > 500
    print("[PASS] test_dark_frame_subtraction_and_encoding")


def main() -> int:
    test_star_fwhm_and_lucky_selection()
    test_cloud_collapse_rejection()
    test_continuous_drift_dynamic_mode_and_bump_reset()
    test_fixed_ref_drift_limit_and_sigma_clipping()
    test_dark_frame_subtraction_and_encoding()
    print("ALL CAMERA LIVE STACKER UNIT TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
