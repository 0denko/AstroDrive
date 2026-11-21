# Changelog

All notable changes to **AstroDrive** are documented in this file.

## [1.3.0] — 2026-11-21

### Licence Change
- **Re-licensed from MIT to GNU General Public License v3.0 (GPLv3)** starting with `v1.3.0` onward (see [`LICENSE`](LICENSE)). Releases prior to `v1.3.0` (`v1.2.0` and earlier) remain under the MIT license as originally published.

### Added — USB Camera & EAA Live Stacking (Pi 3B Tuned, Auto-Installed by Default)
- **V4L2 camera daemon** ([`scripts/onstep_camera.py`](scripts/onstep_camera.py), [`systemd/onstep-camera.service`](systemd/onstep-camera.service), [`udev/99-onstep-camera.rules`](udev/99-onstep-camera.rules)) tuned for **Raspberry Pi 3 Model B** (1 GB RAM, USB 2.0) and standard UVC USB webcams (e.g., disassembled Logitech USB webcam modules) via stable `/dev/astrocam` symlink.
- **Uncompressed YUYV capture default (`640×480`)** with real V4L2 hardware control enumeration, read-back, and one-click **Lock Auto Controls** (`v4l2-ctl`) to kill auto-exposure, auto-gain, auto-WB, and autofocus.
- **Continuous drift auto-alignment (Requirement A):**
  - `dynamic_drift` mode (OpenLiveStacker-style anchored exponential decay average with automatic bump reset and settle-skip for untracked/drifting mounts).
  - `fixed_ref` mode (Welford online sigma-clipped stack with 25% drift guard and "Recentre & Restart Stack" prompt).
- **Lucky-imaging frame selection (Requirement B):** star-FWHM + star-count cloud-collapse grading with blurred-Laplacian fallback for Moon/planets and rolling 200-frame live **Keep Best %** (`25%` / `50%` / `75%` / `100%`) selection.
- **In-RAM / tmpfs pipeline** (zero per-frame microSD card writes): preview JPEG written atomically to `/run/onstep-camera/preview.jpg`, optional master dark-frame capture & subtraction, asinh/black/white display stretch, and user-triggered PNG + JSON metadata save to `/var/lib/onstep-autoprovision/captures`.
- **Clean INDI arbitration:** checks `/proc` open file descriptors via `onstep_common.find_device_holders()` so `onstep-camera.service` backs off cleanly whenever `indi_v4l2_ccd` holds the camera.
- **Strictly authenticated Dashboard Camera UI** in [`webui/index.html`](webui/index.html) and [`webui/onstep_webui.py`](webui/onstep_webui.py): live preview fetched via `X-AstroDrive-Token` blob requests (never embedding tokens in image/download URLs).
- **Auto-installed out of the box** in [`install.sh`](install.sh) and [`.github/workflows/build-pi-image.yml`](.github/workflows/build-pi-image.yml) (pass `--without-camera` to opt out, or toggle off in Dashboard; persisted in `install-meta.env` and forwarded by [`scripts/update_software.sh`](scripts/update_software.sh)).

## [1.2.0] — 2026-11-21
- Added Setup PIN (`X-AstroDrive-Token`) authentication across all mutating endpoints, `GET /api/logs`, and `GET /api/status?probe=1`; redacted unauthenticated `GET /api/status` to minimal pill state; switched printed Setup Wizard links to `#token=` hash fragments.
- Made `AUTO_FLASH="false"` the safe default with 1-click single-use wizard flash tickets and automatic USB serial/path allowlist recording.
- Added daily safety-gated self-updater with automatic rollback (`scripts/update_software.sh`, `systemd/astrodrive-update.timer`), shared `scripts/onstep_common.py`, SHA-256 verification on `update_firmware.sh` and `arduino-cli`, and GitHub Actions lint + security smoke test workflow.

## [1.1.0] — 2026-11-21
- Added one-line `setup.sh`, 5-step browser Setup Wizard, `scripts/configure_indi_defaults.sh`, field Wi-Fi hotspot manager, and verified OnStepX CI builds for ESP32-WROOM and NodeMCU ESP8266 + 2× TMC2208S.
