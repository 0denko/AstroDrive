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
     - Select best-fitting profile (`nodemcu-esp8266-tmc2208s` or `nodemcu-esp32-tmc2208s`).
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

from onstep_common import (
    find_device_holders,
    get_udev_properties,
    read_json_file,
    write_json_atomic,
)
from probe_onstep import probe_port

LOG = logging.getLogger("onstep-autoprovision")
LOCK_PATH = Path("/run/lock/onstep-provision.lock")
STATE_DIR = Path(os.environ.get("ONSTEP_STATE_DIR", "/var/lib/onstep-autoprovision"))
STATUS_FILE = STATE_DIR / "provision-status.json"
FLASH_ONCE_DIR = STATE_DIR / "flash-once"
ONLINE_CHECK_STAMP = STATE_DIR / "last-online-check"
def safe_int(raw: Optional[str], default: int) -> int:
    try:
        return int(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except (ValueError, TypeError):
        return default


def safe_float(raw: Optional[str], default: float) -> float:
    try:
        return float(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except (ValueError, TypeError):
        return default


ONLINE_CHECK_MIN_INTERVAL_SEC = safe_int(os.environ.get("ONLINE_CHECK_MIN_INTERVAL_SEC"), 21600)  # 6 hours
LOCK_TIMEOUT_SEC = safe_float(os.environ.get("ONSTEP_LOCK_TIMEOUT_SEC"), 90.0)

DEFAULT_CHIP_PROFILE_MAP = {
    "esp8266": "nodemcu-esp8266-tmc2208s",
    "esp32": "nodemcu-esp32-tmc2208s",
}


def write_provision_status(state: str, message: str, device: str = "", extra: Optional[dict[str, object]] = None) -> None:
    try:
        payload: dict[str, object] = {
            "state": state,
            "message": message,
            "device": device,
            "updated_at_epoch": int(time.time()),
        }
        if extra:
            payload.update(extra)
        write_json_atomic(STATUS_FILE, payload)
    except OSError:
        pass


def has_valid_flash_once_ticket(device: str) -> bool:
    """Peek at single-use flash authorization ticket without consuming it yet."""
    ticket = FLASH_ONCE_DIR / os.path.basename(device)
    try:
        if ticket.is_file():
            age = time.time() - ticket.stat().st_mtime
            if 0 <= age <= 300:
                return True
            ticket.unlink(missing_ok=True)
            LOG.warning("Removed expired flash authorization ticket for %s (age=%.0fs).", device, age)
    except OSError as exc:
        LOG.warning("Could not inspect flash-once ticket %s: %s", ticket, exc)
    return False


def consume_flash_once_ticket(device: str) -> bool:
    ticket = FLASH_ONCE_DIR / os.path.basename(device)
    try:
        if ticket.is_file():
            age = time.time() - ticket.stat().st_mtime
            ticket.unlink(missing_ok=True)
            if 0 <= age <= 300:
                LOG.info("Consumed single-use flash authorization ticket for %s.", device)
                return True
            LOG.warning("Expired flash authorization ticket for %s (age=%.0fs); ignoring.", device, age)
    except OSError as exc:
        LOG.warning("Could not read/unlink flash-once ticket %s: %s", ticket, exc)
    return False


def acquire_lock_with_timeout(lock_fd, timeout_sec: float = LOCK_TIMEOUT_SEC) -> bool:
    """Acquire exclusive flock with a bounded timeout so waiting units fail visibly (R4)."""
    deadline = time.monotonic() + max(1.0, timeout_sec)
    logged = False
    while True:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if not logged:
                LOG.warning("Another provisioning/update run holds %s; waiting up to %.0fs...", LOCK_PATH, timeout_sec)
                logged = True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


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
    # #13: Try last known-good baud first to avoid walking timeouts on common replugs
    ordered: list[int] = []
    last_status = read_json_file(STATUS_FILE)
    try:
        last_baud = int(str(last_status.get("baud", "")).strip())
        if last_baud > 0:
            ordered.append(last_baud)
    except (ValueError, TypeError):
        pass
    for b in bauds:
        if b not in ordered:
            ordered.append(b)

    for baud in ordered:
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
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        LOG.error("esptool chip_id timed out after 25s on %s.", device)
        return None
    output = f"{res.stdout}\n{res.stderr}"
    if res.returncode != 0:
        LOG.error("esptool chip_id failed on %s: %s", device, output.strip())
        return None

    m = re.search(r"Detecting chip type\.\.\.\s*([A-Za-z0-9\-]+)", output)
    if not m:
        m = re.search(r"Chip is\s+([A-Za-z0-9\-]+)", output)
    if not m:
        # Never substitute configured TARGET_CHIP when esptool output is unparseable
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

    if expected_chip != "auto" and normalized != expected_chip:
        LOG.error("Detected chip %r does not match configured TARGET_CHIP=%r.", normalized, expected_chip)
        return None

    LOG.info("Detected Espressif MCU: %s (normalized: %s)", raw_chip, normalized)
    return normalized


def load_manifest(
    profile_dir: Path,
    expected_profile: Optional[str] = None,
    expected_chip: Optional[str] = None,
) -> tuple[Path, dict[str, str]]:
    """Load and verify firmware manifest.json + binary SHA-256 (R6: strict profile & chip checks)."""
    manifest_path = profile_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Firmware manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bin_name = str(manifest.get("binary") or "").strip()
    expected_sha256 = str(manifest.get("sha256") or "").strip().lower()
    mf_profile = str(manifest.get("profile") or "").strip()
    mf_chip = str(manifest.get("chip") or "").strip().lower()

    if not bin_name or not expected_sha256:
        raise ValueError("manifest.json must contain 'binary' and 'sha256' fields")
    if not mf_profile or not mf_chip:
        raise ValueError("manifest.json must contain 'profile' and 'chip' fields")
    if expected_profile and mf_profile != expected_profile:
        raise ValueError(f"Manifest profile mismatch: expected {expected_profile!r}, got {mf_profile!r}")
    if expected_chip and mf_chip != expected_chip.lower():
        raise ValueError(f"Manifest chip mismatch: expected {expected_chip!r}, got {mf_chip!r}")

    bin_path = (profile_dir / bin_name).resolve()
    if profile_dir.resolve() not in bin_path.parents or not bin_path.is_file():
        raise FileNotFoundError(f"Firmware binary not found: {bin_path}")

    actual_sha256 = hashlib.sha256(bin_path.read_bytes()).hexdigest().lower()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"SHA-256 mismatch for {bin_path}: expected {expected_sha256}, got {actual_sha256}"
        )

    return bin_path, manifest


def try_online_release_fetch(profile: str, firmware_root: Path, force: bool = False) -> bool:
    updater = Path(os.environ.get("UPDATE_SCRIPT", "/opt/onstep-autoprovision/scripts/update_firmware.sh"))
    github_repo = os.environ.get("GITHUB_REPO", "").strip()
    if not github_repo or not updater.is_file():
        return False

    # #6: Avoid redundant GitHub fetches on every USB plug event (throttle to 6 hours)
    if not force and ONLINE_CHECK_STAMP.is_file():
        try:
            age = time.time() - ONLINE_CHECK_STAMP.stat().st_mtime
            if 0 <= age < ONLINE_CHECK_MIN_INTERVAL_SEC:
                LOG.info(
                    "Skipping online release fetch (checked %.0f min ago; throttle=%d s).",
                    age / 60.0,
                    ONLINE_CHECK_MIN_INTERVAL_SEC,
                )
                return False
        except OSError:
            pass

    if not has_internet():
        LOG.info("Pi is offline; skipping GitHub Releases fetch.")
        return False

    LOG.info("Checking GitHub Releases (%s) for latest '%s' firmware...", github_repo, profile)
    try:
        # R5: Align timeout (600s) with update_firmware.sh worst-case curl retry budgets
        res = subprocess.run(
            [str(updater), "--repo", github_repo, "--profile", profile, "--out-dir", str(firmware_root)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("Online release fetch timed out after 600s for profile '%s'.", profile)
        return False

    if res.returncode == 0:
        # Touch throttle stamp ONLY on success so a transient failure never suppresses checks for 6h
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            ONLINE_CHECK_STAMP.touch()
        except OSError:
            pass
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
        LOG.warning("Pi is offline; cannot pull OnStepX for live compilation.")
        return False

    ref_file = builder.parent / "onstepx.ref"
    ref_desc = f"pinned ref ({ref_file.read_text(encoding='utf-8').strip()[:12]})" if ref_file.is_file() else "upstream main (onstepx.ref absent)"
    LOG.info("Compiling OnStepX [%s] for profile '%s' on Pi (this may take several minutes)...", ref_desc, profile)
    env = os.environ.copy()
    env.setdefault("ESPTOOL_PY", f"{sys.executable} -m esptool")
    try:
        res = subprocess.run(
            [str(builder), "--profile", profile, "--out-dir", str(firmware_root)],
            capture_output=True,
            text=True,
            env=env,
            timeout=720,
        )
    except subprocess.TimeoutExpired:
        LOG.error("Live OnStepX compilation timed out after 720s for profile '%s'.", profile)
        return False

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
    expected_chip: str,
    firmware_root: Path,
    online_check: bool,
    auto_compile: bool,
    prefer_live_compile: bool,
    force_online: bool = False,
) -> tuple[Optional[Path], Optional[dict[str, str]]]:
    profile_dir = firmware_root / profile

    def try_local() -> tuple[Optional[Path], Optional[dict[str, str]]]:
        for candidate in (profile_dir, firmware_root):
            try:
                return load_manifest(candidate, expected_profile=profile, expected_chip=expected_chip)
            except Exception:
                continue
        return None, None

    if prefer_live_compile and auto_compile:
        if try_live_compile(profile, firmware_root):
            bp, mf = try_local()
            if bp:
                return bp, mf

    # Check local cache first so we know whether online fetch is mandatory
    bp_local, mf_local = try_local()
    if online_check:
        try_online_release_fetch(profile, firmware_root, force=force_online or (bp_local is None))

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
) -> tuple[bool, str]:
    last_err = ""
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
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            last_err = f"esptool timed out after 180s at {baud} baud"
            LOG.warning("%s on %s.", last_err, device)
            continue
        if res.returncode == 0:
            LOG.info("Firmware flash succeeded at %s baud.", baud)
            return True, ""
        last_err = (res.stderr.strip() or res.stdout.strip() or f"rc={res.returncode}")[-300:]
        LOG.warning("esptool flash failed at %s baud (rc=%d): %s", baud, res.returncode, last_err)
    return False, last_err


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="OnStepX multi-board NodeMCU auto-provisioner")
    parser.add_argument("--device", required=True, help="TTY device path (e.g., /dev/ttyUSB0)")
    parser.add_argument("--dry-run", action="store_true", help="Probe only; never flash or touch symlinks")
    parser.add_argument("--allow-flash-once", action="store_true", help="Authorize flashing this device once even if AUTO_FLASH=false")
    args = parser.parse_args()

    device = args.device
    if not os.path.exists(device):
        LOG.error("Device %s does not exist.", device)
        write_provision_status("error", f"Device {device} does not exist.", device)
        return 1

    target_chip_cfg = os.environ.get("TARGET_CHIP", "auto").strip().lower()
    auto_flash = env_bool("AUTO_FLASH", default=False)
    auto_upgrade = env_bool("AUTO_UPGRADE", default=False)
    online_check = env_bool("ONLINE_CHECK_UPDATES", default=True)
    auto_compile = env_bool("AUTO_COMPILE", default=False)
    prefer_live_compile = env_bool("PREFER_LIVE_COMPILE", default=False)
    firmware_root = Path(os.environ.get("FIRMWARE_DIR", "/opt/onstep-autoprovision/firmware"))
    symlink_path = os.environ.get("SYMLINK_PATH", "/dev/onstep")
    probe_bauds = [safe_int(b, 0) for b in (env_list("PROBE_BAUDS") or ["9600", "115200", "57600", "19200"])]
    probe_bauds = [b for b in probe_bauds if b > 0] or [9600, 115200, 57600, 19200]
    flash_baud = os.environ.get("FLASH_BAUD", "460800").strip() or "460800"
    flash_baud_fallback = os.environ.get("FLASH_BAUD_FALLBACK", "115200").strip() or "115200"
    boot_settle = max(0.0, min(safe_float(os.environ.get("BOOT_SETTLE_SECONDS"), 2.5), 30.0))
    indi_auto_connect = env_bool("INDI_AUTO_CONNECT", default=False)
    indi_device_name = os.environ.get("INDI_DEVICE_NAME", "LX200 OnStep")
    chip_profile_map = parse_chip_profile_map()

    # Peek at ticket without consuming yet so --dry-run / busy / allowlist aborts never burn it
    flash_once_pending = args.allow_flash_once or has_valid_flash_once_ticket(device)

    if not flash_once_pending and not check_usb_allowlist(device):
        write_provision_status("skipped_allowlist", f"Device {device} is not in ALLOWED_USB_SERIALS/ALLOWED_USB_PATHS.", device)
        return 0

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w", encoding="utf-8") as lock_fd:
        if not acquire_lock_with_timeout(lock_fd):
            msg = f"Timed out waiting for provisioner lock on {device} (another flash or update is running)."
            LOG.error("%s", msg)
            write_provision_status("busy", msg, device)
            return 2

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
                    msg = (
                        f"Device {device} is hijacked by {comm} (PID {pid}). "
                        "Ensure USBAUTO=\"false\" in /etc/default/gpsd and ID_MM_DEVICE_IGNORE=1 in udev rules."
                    )
                    LOG.error("%s", msg)
                    write_provision_status("error", msg, device)
                    return 2
            LOG.error("Device %s is busy (held by %s); aborting.", device, holders)
            write_provision_status("busy", f"Device {device} is busy ({holders}).", device)
            return 2

        LOG.info("Waiting %.1fs for controller boot settle on %s...", boot_settle, device)
        time.sleep(boot_settle)

        info = probe_any_baud(device, probe_bauds)
        if info and not auto_upgrade and not flash_once_pending:
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
                write_provision_status(
                    "ready",
                    f"Ready: {info['product']} v{info['version']} @ {info['baud']} baud.",
                    device,
                    {"version": info["version"], "baud": info["baud"]},
                )
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

        # Consume the single-use ticket ONLY here at the actual can_flash decision point
        flash_once_authorized = args.allow_flash_once or consume_flash_once_ticket(device)

        # S4 + M3: Allow flashing if:
        #   - AUTO_FLASH=true, OR
        #   - Explicitly authorized once via the authenticated Setup Wizard, OR
        #   - Board is ALREADY confirmed as OnStepX and AUTO_UPGRADE=true.
        can_flash = auto_flash or flash_once_authorized or (bool(info) and auto_upgrade)
        if not can_flash:
            if info:
                LOG.info("Running OnStepX v%s and AUTO_UPGRADE=false; publishing %s.", info["version"], symlink_path)
                publish_symlink(device, symlink_path)
                write_provision_status("ready", f"Ready: {info['product']} v{info['version']}.", device)
                return 0
            msg = (
                f"Detected non-OnStep board on {device}. Auto-flash is off by default for safety; "
                "click 'Flash OnStepX to This Board' in the Setup Wizard to flash it."
            )
            LOG.info("%s", msg)
            write_provision_status("awaiting_confirmation", msg, device)
            return 0

        # M3 defence-in-depth: warn loudly if unattended AUTO_FLASH is armed with empty allowlists
        if auto_flash and not flash_once_authorized and not env_list("ALLOWED_USB_SERIALS") and not env_list("ALLOWED_USB_PATHS"):
            LOG.warning(
                "SAFETY WARNING: AUTO_FLASH=true with empty ALLOWED_USB_SERIALS/ALLOWED_USB_PATHS — "
                "any CP2102/CH340/CH9102 board plugged into this Pi will be flashed! Populate an allowlist or set AUTO_FLASH=false."
            )

        # Identify the attached Espressif MCU silicon (ESP8266 vs ESP32)
        detected_chip = detect_esp_chip(device, expected_chip=target_chip_cfg)
        if not detected_chip:
            msg = (
                f"Could not identify an ESP32/ESP8266 on {device}. "
                "If this is an ESP32, hold the BOOT button for 2 seconds and click Flash again."
            )
            LOG.error("%s", msg)
            write_provision_status("detect_failed", msg, device)
            return 1

        profile = chip_profile_map.get(detected_chip)
        if not profile:
            msg = (
                f"Detected '{detected_chip}' on {device}, which is not supported by AstroDrive's "
                f"TMC2208S profiles (supported: {', '.join(sorted(chip_profile_map))})."
            )
            LOG.error("%s", msg)
            write_provision_status("unsupported_chip", msg, device, {"chip": detected_chip})
            return 1

        LOG.info("Selected firmware profile '%s' for chip '%s'.", profile, detected_chip)

        bin_path, manifest = resolve_firmware_bundle(
            profile=profile,
            expected_chip=detected_chip,
            firmware_root=firmware_root,
            online_check=online_check,
            auto_compile=auto_compile,
            prefer_live_compile=prefer_live_compile,
            force_online=flash_once_authorized or auto_upgrade,
        )
        if not bin_path or not manifest:
            msg = (
                f"No verified cached firmware found for '{profile}' ({detected_chip}). Connect the Pi to the internet "
                "and click 'Download Firmware' in Setup Wizard Step 1."
            )
            LOG.error("%s", msg)
            write_provision_status("missing_firmware", msg, device, {"chip": detected_chip, "profile": profile})
            if info:
                LOG.warning("Falling back to existing OnStepX v%s on %s.", info["version"], device)
                publish_symlink(device, symlink_path)
                return 0
            return 1

        target_version = str(manifest.get("firmware_version", "")).strip()
        if info and target_version and info["version"] == target_version and not flash_once_authorized:
            LOG.info("Board already runs latest OnStepX v%s (%s); skipping flash.", target_version, profile)
            publish_symlink(device, symlink_path)
            write_provision_status("ready", f"Already up to date: OnStepX v{target_version} ({profile}).", device)
            if indi_auto_connect:
                notify_indi_connect(indi_device_name)
            return 0

        flash_offset = manifest.get("flash_offset", "0x00000" if detected_chip == "esp8266" else "0x0")
        flash_mode = manifest.get("flash_mode", "dio")

        flashed, flash_err = flash_firmware(
            device=device,
            bin_path=bin_path,
            target_chip=detected_chip,
            flash_offset=flash_offset,
            flash_mode=flash_mode,
            primary_baud=flash_baud,
            fallback_baud=flash_baud_fallback,
        )
        if not flashed:
            msg = f"Flashing {profile} failed on {device} ({flash_err or 'check USB cable / hold BOOT on ESP32'})."
            LOG.error("%s", msg)
            write_provision_status("flash_failed", msg, device, {"chip": detected_chip, "profile": profile})
            return 1

        LOG.info("Waiting 3.0s for freshly flashed OnStepX (%s) to boot...", profile)
        time.sleep(3.0)

        post_info = probe_any_baud(device, probe_bauds)
        if not post_info:
            msg = f"Flashed {profile} on {device}, but OnStepX did not answer :GVP# after reset."
            LOG.error("%s", msg)
            write_provision_status("verify_failed", msg, device, {"chip": detected_chip, "profile": profile})
            return 1

        LOG.info(
            "Post-flash verification PASSED: %s v%s (%s) @ %s baud.",
            post_info["product"],
            post_info["version"],
            profile,
            post_info["baud"],
        )
        publish_symlink(device, symlink_path)
        write_provision_status(
            "ready",
            f"Flashed & verified: {post_info['product']} v{post_info['version']} ({profile}) @ {post_info['baud']} baud.",
            device,
            {"chip": detected_chip, "profile": profile, "version": post_info["version"]},
        )
        if indi_auto_connect:
            notify_indi_connect(indi_device_name)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
