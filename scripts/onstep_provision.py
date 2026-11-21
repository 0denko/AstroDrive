#!/usr/bin/env python3
"""
Automated OnStepX detection, MCU auto-identification, online fetch / live compile,
safety-gated esptool flashing, and INDI symlink publisher for Raspberry Pi.

Supports ANY random NodeMCU board (ESP8266 or ESP32):
  1. Acquire exclusive flock (/run/lock/onstep-provision.lock).
  2. Enforce optional USB serial / physical port allowlist from config.env.
  3. Inspect /proc/*/fd so we NEVER disrupt an active indi_lx200_OnStep session.
  4. Probe LX200 :GVP# / :GVN# without DTR/RTS reset.
     - If already running OnStepX and no upgrade required -> publish /dev/onstep & exit.
  5. If blank / non-OnStep (or upgrade requested):
     - Auto-detect exact MCU silicon via `esptool --chip auto chip_id` (esp8266 vs esp32).
     - Select best-fitting profile (`nodemcu-esp8266-tmc2209s` or `nodemcu-esp32-tmc2209s`).
     - Resolve firmware via 3 tiers:
         Tier 1: Online GitHub Releases fetch (`update_firmware.sh`) if online & configured.
         Tier 2: Verified local cache (`/opt/onstep-autoprovision/firmware/<profile>/`).
         Tier 3: Pull latest hjd1964/OnStepX & live-compile on Pi via `arduino-cli` (`build.sh`)
                 if `AUTO_COMPILE=true` and no verified binary is cached (or `PREFER_LIVE_COMPILE=true`).
     - Verify SHA-256, flash via esptool (460800 -> 115200 fallback), hard-reset,
       re-verify :GVP# -> On-Step#, and atomically publish /dev/onstep.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from probe_onstep import probe_port

LOG = logging.getLogger("onstep-autoprovision")
LOCK_PATH = Path("/run/lock/onstep-provision.lock")

DEFAULT_CHIP_PROFILE_MAP = {
    "esp8266": "nodemcu-esp8266-tmc2209s",
    "esp32": "nodemcu-esp32-tmc2209s",
}


def env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_chip_profile_map() -> dict[str, str]:
    mapping = dict(DEFAULT_CHIP_PROFILE_MAP)
    raw = os.environ.get("CHIP_PROFILE_MAP", "").strip()
    for pair in raw.split(","):
        if "=" in pair:
            chip, prof = pair.split("=", 1)
            chip, prof = chip.strip().lower(), prof.strip()
            if chip and prof:
                mapping[chip] = prof
    return mapping


def has_internet(host: str = "github.com", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_udev_properties(device: str) -> dict[str, str]:
    props: dict[str, str] = {}
    try:
        res = subprocess.run(
            ["udevadm", "info", "-q", "property", "-n", device],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
    except Exception as exc:
        LOG.warning("Failed to query udev properties for %s: %s", device, exc)
    return props


def check_usb_allowlist(device: str) -> bool:
    allowed_serials = env_list("ALLOWED_USB_SERIALS")
    allowed_paths = env_list("ALLOWED_USB_PATHS")
    if not allowed_serials and not allowed_paths:
        return True

    props = get_udev_properties(device)
    serial_short = props.get("ID_SERIAL_SHORT", "")
    id_path = props.get("ID_PATH", "")

    if allowed_serials and serial_short in allowed_serials:
        LOG.info("Device %s matched ALLOWED_USB_SERIALS (%s)", device, serial_short)
        return True
    if allowed_paths and id_path in allowed_paths:
        LOG.info("Device %s matched ALLOWED_USB_PATHS (%s)", device, id_path)
        return True

    LOG.warning(
        "Device %s (serial=%r, path=%r) is NOT in ALLOWED_USB_SERIALS/ALLOWED_USB_PATHS; skipping.",
        device,
        serial_short,
        id_path,
    )
    return False


def find_device_holders(device: str) -> list[tuple[int, str]]:
    """Return list of (pid, comm) holding an open FD to `device`."""
    target = os.path.realpath(device)
    my_pid = os.getpid()
    holders: list[tuple[int, str]] = []

    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        fd_dir = Path(entry.path) / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if os.path.realpath(str(fd)) == target:
                        comm = (Path(entry.path) / "comm").read_text(errors="replace").strip()
                        holders.append((pid, comm))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return holders


def publish_symlink(device: str, symlink_path: str) -> None:
    target = os.path.realpath(device)
    link = Path(symlink_path)
    tmp_link = Path(f"{symlink_path}.tmp.{os.getpid()}")
    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        os.symlink(target, tmp_link)
        os.replace(tmp_link, link)
        LOG.info("Published symlink %s -> %s", symlink_path, target)
    finally:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()


def notify_indi_connect(device_name: str) -> None:
    indi_setprop = shutil.which("indi_setprop")
    if not indi_setprop:
        LOG.debug("indi_setprop not found; skipping INDI reconnect notification.")
        return
    prop = f"{device_name}.CONNECTION.CONNECT=On"
    try:
        res = subprocess.run([indi_setprop, prop], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            LOG.info("Sent INDI reconnect request: %s", prop)
        else:
            LOG.debug("indi_setprop returned %d (%s)", res.returncode, res.stderr.strip())
    except Exception as exc:
        LOG.debug("indi_setprop failed (indiserver may not be running): %s", exc)


def probe_any_baud(device: str, bauds: list[int]) -> Optional[dict[str, str]]:
    for baud in bauds:
        try:
            info = probe_port(device, baud, timeout=1.5)
            if info:
                return info
        except Exception as exc:
            LOG.warning("Serial probe error on %s @ %d baud: %s", device, baud, exc)
    return None


def detect_esp_chip(device: str, expected_chip: str = "auto") -> Optional[str]:
    """Identify attached Espressif MCU silicon via esptool (`esp8266`, `esp32`, etc.)."""
    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        expected_chip,
        "--port",
        device,
        "--baud",
        "115200",
        "chip_id",
    ]
    LOG.info("Detecting MCU silicon via esptool (--chip %s) on %s...", expected_chip, device)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    output = f"{res.stdout}\n{res.stderr}"
    if res.returncode != 0:
        LOG.error("esptool chip_id failed on %s: %s", device, output.strip())
        return None

    m = re.search(r"Detecting chip type\.\.\.\s*([A-Za-z0-9\-]+)", output)
    if not m:
        m = re.search(r"Chip is\s+([A-Za-z0-9\-]+)", output)
    if not m:
        if expected_chip != "auto":
            return expected_chip
        LOG.error("Could not parse chip type from esptool output: %s", output.strip())
        return None

    raw_chip = m.group(1).lower()
    if raw_chip.startswith("esp8266"):
        normalized = "esp8266"
    elif raw_chip.startswith("esp32-s3") or raw_chip.startswith("esp32s3"):
        normalized = "esp32s3"
    elif raw_chip.startswith("esp32-c3") or raw_chip.startswith("esp32c3"):
        normalized = "esp32c3"
    elif raw_chip.startswith("esp32"):
        normalized = "esp32"
    else:
        normalized = raw_chip

    LOG.info("Detected Espressif MCU: %s (normalized: %s)", raw_chip, normalized)
    return normalized


def load_manifest(profile_dir: Path) -> tuple[Path, dict[str, str]]:
    manifest_path = profile_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Firmware manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bin_name = manifest.get("binary")
    expected_sha256 = manifest.get("sha256", "").lower()
    if not bin_name or not expected_sha256:
        raise ValueError("manifest.json must contain 'binary' and 'sha256' fields")

    bin_path = profile_dir / bin_name
    if not bin_path.is_file():
        raise FileNotFoundError(f"Firmware binary not found: {bin_path}")

    actual_sha256 = hashlib.sha256(bin_path.read_bytes()).hexdigest().lower()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"SHA-256 mismatch for {bin_path}: expected {expected_sha256}, got {actual_sha256}"
        )

    return bin_path, manifest


def try_online_release_fetch(profile: str, firmware_root: Path) -> bool:
    updater = Path(os.environ.get("UPDATE_SCRIPT", "/opt/onstep-autoprovision/scripts/update_firmware.sh"))
    github_repo = os.environ.get("GITHUB_REPO", "").strip()
    if not github_repo or not updater.is_file():
        return False
    if not has_internet():
        LOG.info("Pi is offline; skipping GitHub Releases fetch.")
        return False

    LOG.info("Checking GitHub Releases (%s) for latest '%s' firmware...", github_repo, profile)
    res = subprocess.run(
        [str(updater), "--repo", github_repo, "--profile", profile, "--out-dir", str(firmware_root)],
        capture_output=True,
        text=True,
        timeout=150,
    )
    if res.returncode == 0:
        if res.stdout.strip():
            LOG.info("%s", res.stdout.strip())
        return True
    LOG.warning("Online release fetch failed (rc=%d): %s", res.returncode, res.stderr.strip() or res.stdout.strip())
    return False


def try_live_compile(profile: str, firmware_root: Path) -> bool:
    builder = Path(os.environ.get("BUILD_SCRIPT", "/opt/onstep-autoprovision/firmware-src/build.sh"))
    if not builder.is_file():
        LOG.warning("Live compile requested but build script not found at %s.", builder)
        return False
    if not shutil.which("arduino-cli"):
        LOG.warning("Live compile requested but `arduino-cli` is not installed on PATH.")
        return False
    if not has_internet():
        LOG.warning("Pi is offline; cannot pull latest OnStepX for live compilation.")
        return False

    LOG.info("Pulling latest upstream OnStepX and compiling profile '%s' on Pi (this may take several minutes)...", profile)
    env = os.environ.copy()
    env.setdefault("ESPTOOL_PY", f"{sys.executable} -m esptool")
    res = subprocess.run(
        [str(builder), "--profile", profile, "--out-dir", str(firmware_root)],
        capture_output=True,
        text=True,
        env=env,
        timeout=720,
    )
    if res.returncode == 0:
        LOG.info("Live OnStepX compilation succeeded for profile '%s'.", profile)
        return True
    LOG.error(
        "Live OnStepX compilation failed (rc=%d):\n%s\n%s",
        res.returncode,
        res.stdout[-1500:],
        res.stderr[-1500:],
    )
    return False


def resolve_firmware_bundle(
    profile: str,
    firmware_root: Path,
    online_check: bool,
    auto_compile: bool,
    prefer_live_compile: bool,
) -> tuple[Optional[Path], Optional[dict[str, str]]]:
    profile_dir = firmware_root / profile

    # Support legacy flat layout (/opt/onstep-autoprovision/firmware/manifest.json) as fallback
    def try_local() -> tuple[Optional[Path], Optional[dict[str, str]]]:
        for candidate in (profile_dir, firmware_root):
            try:
                bp, mf = load_manifest(candidate)
                if mf.get("profile", profile) == profile:
                    return bp, mf
            except Exception:
                continue
        return None, None

    if prefer_live_compile and auto_compile:
        if try_live_compile(profile, firmware_root):
            bp, mf = try_local()
            if bp:
                return bp, mf

    if online_check:
        try_online_release_fetch(profile, firmware_root)

    bp, mf = try_local()
    if bp:
        return bp, mf

    if auto_compile:
        if try_live_compile(profile, firmware_root):
            return try_local()

    return None, None


def flash_firmware(
    device: str,
    bin_path: Path,
    target_chip: str,
    flash_offset: str,
    flash_mode: str,
    primary_baud: str,
    fallback_baud: str,
) -> bool:
    for baud in [primary_baud, fallback_baud]:
        cmd = [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            target_chip,
            "--port",
            device,
            "--baud",
            baud,
            "--before",
            "default_reset",
            "--after",
            "hard_reset",
            "write_flash",
            "--flash_mode",
            flash_mode,
            "--flash_size",
            "detect",
            flash_offset,
            str(bin_path),
        ]
        LOG.info(
            "Flashing %s (%s @ %s, mode=%s) to %s at %s baud...",
            bin_path.name,
            target_chip,
            flash_offset,
            flash_mode,
            device,
            baud,
        )
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            LOG.info("Firmware flash succeeded at %s baud.", baud)
            return True
        LOG.warning(
            "esptool flash failed at %s baud (rc=%d): %s",
            baud,
            res.returncode,
            res.stderr.strip() or res.stdout.strip(),
        )
    return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="OnStepX multi-board NodeMCU auto-provisioner")
    parser.add_argument("--device", required=True, help="TTY device path (e.g., /dev/ttyUSB0)")
    parser.add_argument("--dry-run", action="store_true", help="Probe only; never flash or touch symlinks")
    args = parser.parse_args()

    device = args.device
    if not os.path.exists(device):
        LOG.error("Device %s does not exist.", device)
        return 1

    target_chip_cfg = os.environ.get("TARGET_CHIP", "auto").strip().lower()
    auto_flash = env_bool("AUTO_FLASH", default=False)
    auto_upgrade = env_bool("AUTO_UPGRADE", default=False)
    online_check = env_bool("ONLINE_CHECK_UPDATES", default=True)
    auto_compile = env_bool("AUTO_COMPILE", default=True)
    prefer_live_compile = env_bool("PREFER_LIVE_COMPILE", default=False)
    firmware_root = Path(os.environ.get("FIRMWARE_DIR", "/opt/onstep-autoprovision/firmware"))
    symlink_path = os.environ.get("SYMLINK_PATH", "/dev/onstep")
    probe_bauds = [int(b) for b in env_list("PROBE_BAUDS") or ["9600", "115200", "57600", "19200"]]
    flash_baud = os.environ.get("FLASH_BAUD", "460800")
    flash_baud_fallback = os.environ.get("FLASH_BAUD_FALLBACK", "115200")
    boot_settle = float(os.environ.get("BOOT_SETTLE_SECONDS", "2.5"))
    indi_auto_connect = env_bool("INDI_AUTO_CONNECT", default=False)
    indi_device_name = os.environ.get("INDI_DEVICE_NAME", "LX200 OnStep")
    chip_profile_map = parse_chip_profile_map()

    if not check_usb_allowlist(device):
        return 0

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w", encoding="utf-8") as lock_fd:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.warning("Another provisioning run holds %s; waiting...", LOCK_PATH)
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)

        # Guard against disrupting an active INDI driver or colliding with gpsd/ModemManager
        holders = find_device_holders(device)
        if holders:
            for pid, comm in holders:
                if "indi_lx200" in comm or "indiserver" in comm:
                    LOG.info(
                        "Device %s is already actively held by %s (PID %d); aborting safely.",
                        device,
                        comm,
                        pid,
                    )
                    if not args.dry_run and not os.path.exists(symlink_path):
                        publish_symlink(device, symlink_path)
                    return 0
                if comm in {"gpsd", "ModemManager"}:
                    LOG.error(
                        "Device %s is hijacked by %s (PID %d)! Set USBAUTO=\"false\" in /etc/default/gpsd "
                        "and ensure ID_MM_DEVICE_IGNORE=1 in udev rules.",
                        device,
                        comm,
                        pid,
                    )
                    return 2
            LOG.error("Device %s is busy (held by %s); aborting.", device, holders)
            return 2

        LOG.info("Waiting %.1fs for NodeMCU boot settle on %s...", boot_settle, device)
        time.sleep(boot_settle)

        info = probe_any_baud(device, probe_bauds)
        if info and not auto_upgrade:
            LOG.info(
                "Detected working %s v%s (built %s) on %s @ %s baud.",
                info["product"],
                info["version"],
                info["date"],
                device,
                info["baud"],
            )
            if not args.dry_run:
                publish_symlink(device, symlink_path)
                if indi_auto_connect:
                    notify_indi_connect(indi_device_name)
            return 0

        if args.dry_run:
            LOG.info(
                "[DRY-RUN] OnStep probe=%s, AUTO_UPGRADE=%s -> would inspect chip & resolve firmware.",
                info,
                auto_upgrade,
            )
            return 0 if info else 3

        if not auto_flash:
            if info:
                LOG.info("Running OnStepX v%s and AUTO_FLASH=false; publishing %s.", info["version"], symlink_path)
                publish_symlink(device, symlink_path)
                return 0
            LOG.warning(
                "No OnStepX response on %s and AUTO_FLASH=false; refusing to flash. "
                "Set AUTO_FLASH=true in /etc/onstep-autoprovision/config.env to enable.",
                device,
            )
            return 1

        # Identify the attached random NodeMCU silicon (ESP8266 vs ESP32)
        detected_chip = detect_esp_chip(device, expected_chip=target_chip_cfg)
        if not detected_chip:
            LOG.error("Refusing to flash %s: could not identify supported Espressif chip.", device)
            return 1

        profile = chip_profile_map.get(detected_chip)
        if not profile:
            LOG.error(
                "No firmware profile mapped for detected chip '%s' (map: %s).",
                detected_chip,
                chip_profile_map,
            )
            return 1

        LOG.info("Selected best-fitting firmware profile '%s' for chip '%s'.", profile, detected_chip)

        bin_path, manifest = resolve_firmware_bundle(
            profile=profile,
            firmware_root=firmware_root,
            online_check=online_check,
            auto_compile=auto_compile,
            prefer_live_compile=prefer_live_compile,
        )
        if not bin_path or not manifest:
            LOG.error("Unable to fetch, locate, or compile verified firmware for profile '%s'.", profile)
            if info:
                LOG.warning("Falling back to existing OnStepX v%s on %s.", info["version"], device)
                publish_symlink(device, symlink_path)
                return 0
            return 1

        target_version = str(manifest.get("firmware_version", "")).strip()
        if info and target_version and info["version"] == target_version:
            LOG.info("Board already runs latest OnStepX v%s (%s); skipping flash.", target_version, profile)
            publish_symlink(device, symlink_path)
            if indi_auto_connect:
                notify_indi_connect(indi_device_name)
            return 0

        flash_offset = manifest.get("flash_offset", "0x00000" if detected_chip == "esp8266" else "0x0")
        flash_mode = manifest.get("flash_mode", "dio")

        if not flash_firmware(
            device=device,
            bin_path=bin_path,
            target_chip=detected_chip,
            flash_offset=flash_offset,
            flash_mode=flash_mode,
            primary_baud=flash_baud,
            fallback_baud=flash_baud_fallback,
        ):
            LOG.error("Firmware flash failed on %s.", device)
            return 1

        LOG.info("Waiting 3.0s for freshly flashed OnStepX (%s) to boot...", profile)
        time.sleep(3.0)

        post_info = probe_any_baud(device, probe_bauds)
        if not post_info:
            LOG.error("Post-flash verification failed: no :GVP# response on %s.", device)
            return 1

        LOG.info(
            "Post-flash verification PASSED: %s v%s (%s) @ %s baud.",
            post_info["product"],
            post_info["version"],
            profile,
            post_info["baud"],
        )
        publish_symlink(device, symlink_path)
        if indi_auto_connect:
            notify_indi_connect(indi_device_name)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
