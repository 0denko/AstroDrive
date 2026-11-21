#!/usr/bin/env python3
"""
Unit test suite for AstroDrive's EAA LiveStacker (scripts/onstep_camera.py),
config hardening (scripts/onstep_common.py, R1), and manifest validation
(scripts/onstep_provision.py, R6).
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import onstep_camera as cam  # noqa: E402
import onstep_common as common  # noqa: E402

try:
    import serial  # noqa: F401
except ImportError:
    import types
    _stub = types.ModuleType("serial")
    _stub.Serial = object  # type: ignore[attr-defined]
    _stub.SerialException = OSError  # type: ignore[attr-defined]
    sys.modules["serial"] = _stub

import onstep_provision as prov  # noqa: E402


def make_starfield(
    h: int = 160,
    w: int = 200,
    shift_x: float = 0.0,
    shift_y: float = 0.0,
    sigma: float = 1.6,
    bg: float = 18.0,
    hot_pixel: tuple[int, int] | None = None,
    streak_row: int | None = None,
) -> np.ndarray:
    """Render a deterministic synthetic RGB starfield with sub-pixel shifted Gaussian stars."""
    stars = [
        (0.22 * w, 0.25 * h, 210.0),
        (0.47 * w, 0.32 * h, 185.0),
        (0.72 * w, 0.28 * h, 200.0),
        (0.30 * w, 0.60 * h, 190.0),
        (0.58 * w, 0.64 * h, 220.0),
        (0.79 * w, 0.74 * h, 175.0),
        (0.39 * w, 0.81 * h, 195.0),
        (0.66 * w, 0.46 * h, 180.0),
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
    if streak_row is not None:
        img[streak_row, :] = 245.0
    rgb = np.clip(np.stack([img, img * 0.98, img * 0.95], axis=-1), 0, 255).astype(np.uint8)
    return rgb


def make_textured_planet(h: int = 160, w: int = 200, contrast: float = 40.0) -> np.ndarray:
    """Render a synthetic banded planetary/lunar disc (no point stars -> Laplacian metric)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cy, cx = h / 2.0, w / 2.0
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    img = np.full((h, w), 10.0, dtype=np.float32)
    disc = r < 52.0
    # Smooth limb darkening + horizontal cloud bands (no point-source peaks)
    limb = np.clip(1.0 - (r / 52.0) ** 2, 0.0, 1.0)
    bands = 150.0 * np.sqrt(limb) + contrast * np.sin((yy - cy) * 0.28)
    img[disc] = np.clip(bands[disc], 20.0, 235.0)
    return np.clip(np.stack([img, img * 0.96, img * 0.88], axis=-1), 0, 255).astype(np.uint8)


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

    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 50})
    for _ in range(8):
        st.process_frame(sharp)
    kept_after_sharp = st.frames_kept
    for _ in range(8):
        st.process_frame(blurry)
    assert kept_after_sharp >= 7, kept_after_sharp
    assert st.rejected_quality >= 6, st.metrics_dict()
    print("[PASS] test_star_fwhm_and_lucky_selection")


def test_cloud_rejection_and_sparse_field_recovery() -> None:
    """Verify R9: temporary clouds are rejected, but slewing to a sparse field recovers cleanly."""
    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 100})
    sharp = make_starfield(sigma=1.5)
    for _ in range(6):
        st.process_frame(sharp)
    kept_before = st.frames_kept

    # Brief passing cloud -> rejected
    clouds = np.full_like(sharp, 25)
    st.process_frame(clouds)
    assert st.rejected_clouds == 1, st.metrics_dict()
    assert st.frames_kept == kept_before

    # Prolonged sparse/extended target (15 frames) -> auto-clears baseline instead of permanent lockout
    planet = make_textured_planet(contrast=45.0)
    for _ in range(16):
        st.process_frame(planet)
    assert st.consecutive_cloud_rejections == 0, st.metrics_dict()
    assert st.last_frame_action != "rejected_clouds", st.metrics_dict()
    print("[PASS] test_cloud_rejection_and_sparse_field_recovery")


def test_separate_metric_score_histories() -> None:
    """Verify R10: star FWHM scores and Laplacian scores never contaminate each other's thresholds."""
    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 50})
    planet_high = make_textured_planet(contrast=70.0)
    for _ in range(12):
        st.process_frame(planet_high)
    assert len(st.score_history_lap) == 12
    assert len(st.score_history_star) == 0
    lap_thresh = st._compute_threshold("laplacian")
    star_thresh = st._compute_threshold("star_fwhm")
    # High Laplacian scores (hundreds/thousands) must NOT raise star_fwhm threshold
    assert lap_thresh > 5.0, lap_thresh
    assert star_thresh == float(st.settings["quality_floor"]), (star_thresh, lap_thresh)

    # Low-texture reference frame must always be acquired even below quality_floor
    st2 = cam.LiveStacker({"mode": "fixed_ref", "keep_pct": 100, "quality_floor": 50.0})
    dim_flat = np.full((120, 160, 3), 20, dtype=np.uint8)
    st2.process_frame(dim_flat)
    assert st2.frames_kept == 1 and st2.last_frame_action == "reference_acquired", st2.metrics_dict()
    print("[PASS] test_separate_metric_score_histories")


def test_continuous_drift_binned_640x480_and_bump_reset() -> None:
    """Verify binning (640x480 -> factor=2.0), continuous drift tracking, and bump reset + settle-skip."""
    st = cam.LiveStacker({"mode": "dynamic_drift", "keep_pct": 100, "settle_skip_frames": 2})
    for i in range(8):
        frame = make_starfield(h=480, w=640, shift_x=4.0 * i, shift_y=2.0 * i, sigma=2.2)
        st.process_frame(frame, render_preview=(i == 7))
    m = st.metrics_dict(exposure_sec=0.2)
    assert st.align_scale == 2.0, st.align_scale
    assert m["frames_kept"] >= 7, m
    assert abs(m["drift_x_px"] - 28.0) < 4.5, m
    assert abs(m["drift_y_px"] - 14.0) < 4.5, m
    assert m["effective_exposure_sec"] >= 1.4, m

    # Sudden 120 px mount bump -> auto-reset + settle-skip for 2 frames
    bumped = make_starfield(h=480, w=640, shift_x=148.0, shift_y=14.0, sigma=2.2)
    st.process_frame(bumped)
    assert "bump_reset" in st.last_frame_action, st.last_frame_action
    assert st.frames_kept == 0
    st.process_frame(bumped)
    assert "settling" in st.last_frame_action, st.last_frame_action
    st.process_frame(bumped)
    assert "settling" in st.last_frame_action, st.last_frame_action
    st.process_frame(bumped)
    assert st.frames_kept == 1, st.metrics_dict()
    print("[PASS] test_continuous_drift_binned_640x480_and_bump_reset")


def test_fixed_ref_winsorised_sigma_clipping_and_drift_limit() -> None:
    """Verify float64 Winsorised Welford sigma-clipping (satellite streak) AND 25% drift guard + recovery."""
    st = cam.LiveStacker({"mode": "fixed_ref", "keep_pct": 100, "drift_limit_pct": 25.0, "sigma_clip": 2.5})
    rng = np.random.default_rng(42)
    for i in range(7):
        base = make_starfield(shift_x=1.5 * i, shift_y=0.0, sigma=1.6).astype(np.float32)
        noise = rng.normal(0.0, 2.0, size=base.shape).astype(np.float32)
        frame = np.clip(base + noise, 0, 255).astype(np.uint8)
        st.process_frame(frame, render_preview=False)
    assert st.frames_kept == 7 and st.accumulator is not None and st.accumulator.dtype == np.float64
    assert st.m2 is not None and float(st.m2.mean()) > 0.0

    # Frame 8: bright horizontal satellite streak across row 15 (empty sky background ~18)
    streak_frame = make_starfield(shift_x=10.5, shift_y=0.0, sigma=1.6, streak_row=15)
    before_row = float(st.accumulator[15, 100, 0])
    st.process_frame(streak_frame, render_preview=False)
    after_row = float(st.accumulator[15, 100, 0])
    # Without clipping, averaging 245 into 7 frames of ~18 jumps by (245-18)/8 = +28.4.
    # Winsorised sigma-clipping must keep the jump tiny (<4 DN).
    assert (after_row - before_row) < 4.0, (before_row, after_row)

    # Shift beyond 25% of 200 px (>50 px) -> triggers drift_limit_reached
    far = make_starfield(shift_x=65.0, shift_y=0.0, sigma=1.6)
    st.process_frame(far)
    assert st.drift_limit_reached is True, st.metrics_dict()
    assert st.rejected_drift_limit >= 1, st.metrics_dict()

    # Drift returning inside 25% clears drift_limit_reached cleanly
    back = make_starfield(shift_x=6.0, shift_y=0.0, sigma=1.6)
    st.process_frame(back)
    assert st.drift_limit_reached is False, st.metrics_dict()
    print("[PASS] test_fixed_ref_winsorised_sigma_clipping_and_drift_limit")


def test_dark_frame_settings_coercion_and_encoding() -> None:
    hot = (20, 25)
    dark_raw = np.full((160, 200, 3), 18, dtype=np.uint8)
    dark_raw[hot[0], hot[1]] = [255, 255, 255]

    st = cam.LiveStacker({"mode": "fixed_ref", "keep_pct": 100})
    # Malformed settings must never crash or corrupt mode/sigma_clip
    st.update_settings({"mode": "bogus_mode", "keep_pct": "high", "sigma_clip": -5.0, "black_point": 0.9, "white_point": 0.1})
    assert st.settings["mode"] == "fixed_ref"
    assert st.settings["sigma_clip"] >= 0.5
    assert st.settings["white_point"] > st.settings["black_point"]

    st.start_dark_capture(count=4)
    for _ in range(4):
        st.process_frame(dark_raw)
    assert st.dark_frame is not None and st.dark_count == 4, st.metrics_dict()

    sci = make_starfield(sigma=1.6, bg=18.0, hot_pixel=hot)
    disp = st.process_frame(sci)
    assert disp is not None and st.accumulator is not None
    assert float(st.accumulator[hot[0], hot[1]].mean()) < 5.0, st.accumulator[hot[0], hot[1]]

    jpg = cam.encode_jpeg(disp, quality=80)
    assert len(jpg) > 500 and jpg[:2] == b"\xff\xd8"
    with tempfile.TemporaryDirectory() as td:
        out_png = Path(td) / "test.png"
        cam.save_png(disp, out_png)
        raw_png = out_png.read_bytes()
        assert raw_png[:8] == b"\x89PNG\r\n\x1a\n" and len(raw_png) > 100
    print("[PASS] test_dark_frame_settings_coercion_and_encoding")


def test_r1_config_escaping_and_usb_id_validation() -> None:
    """Verify R1: config.env injection payloads are rejected/escaped cleanly."""
    assert common.is_safe_usb_id("0001A2B3") is True
    assert common.is_safe_usb_id("platform-3f980000.usb-usb-0:1.2:1.0") is True
    for bad in ('evil"\nAUTO_FLASH="true', "$(whoami)", "a`id`b", "has space", ""):
        assert common.is_safe_usb_id(bad) is False, bad

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.env"
        common.write_config_keys(cfg, {"TEST_VAL": 'a"b$c`d\\e'})
        raw = cfg.read_text(encoding="utf-8")
        assert '\\"' in raw and "\\$" in raw and "\\`" in raw, raw
        parsed = common.parse_env_file(cfg)
        assert parsed["TEST_VAL"] == 'a"b$c`d\\e', parsed

        try:
            common.write_config_keys(cfg, {"BAD": "line1\nline2"})
            raise AssertionError("Expected ValueError on newline in config value")
        except ValueError:
            pass
    print("[PASS] test_r1_config_escaping_and_usb_id_validation")


def test_r6_strict_manifest_validation() -> None:
    """Verify R6: load_manifest rejects missing or mismatched profile/chip/sha256."""
    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        bin_bytes = b"dummy-onstep-firmware-bytes"
        (pdir / "fw.bin").write_bytes(bin_bytes)
        good_sha = hashlib.sha256(bin_bytes).hexdigest()

        # Good manifest passes
        (pdir / "manifest.json").write_text(json.dumps({
            "profile": "nodemcu-esp32-tmc2208s",
            "chip": "esp32",
            "binary": "fw.bin",
            "sha256": good_sha,
        }))
        bp, mf = prov.load_manifest(pdir, expected_profile="nodemcu-esp32-tmc2208s", expected_chip="esp32")
        assert bp.name == "fw.bin" and mf["chip"] == "esp32"

        # Wrong chip rejected
        try:
            prov.load_manifest(pdir, expected_profile="nodemcu-esp32-tmc2208s", expected_chip="esp8266")
            raise AssertionError("Expected ValueError on chip mismatch")
        except ValueError:
            pass

        # Missing profile/chip rejected (closes legacy bypass)
        (pdir / "manifest.json").write_text(json.dumps({"binary": "fw.bin", "sha256": good_sha}))
        try:
            prov.load_manifest(pdir, expected_profile="nodemcu-esp32-tmc2208s", expected_chip="esp32")
            raise AssertionError("Expected ValueError on missing profile/chip")
        except ValueError:
            pass
    print("[PASS] test_r6_strict_manifest_validation")


def main() -> int:
    test_star_fwhm_and_lucky_selection()
    test_cloud_rejection_and_sparse_field_recovery()
    test_separate_metric_score_histories()
    test_continuous_drift_binned_640x480_and_bump_reset()
    test_fixed_ref_winsorised_sigma_clipping_and_drift_limit()
    test_dark_frame_settings_coercion_and_encoding()
    test_r1_config_escaping_and_usb_id_validation()
    test_r6_strict_manifest_validation()
    print("ALL UNIT TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
