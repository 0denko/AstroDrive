#!/usr/bin/env python3
"""
Lightweight, zero-dependency Web UI, Setup Wizard & REST API for AstroDrive.

Runs on port 8090 (exposed inside Astroberry OS at http://astroberry.local/onstep/
via Nginx overlay, and also accessible directly at http://<pi-ip>:8090/).

Security model (M1):
  - All mutating POST routes, GET /api/logs, and GET /api/status?probe=1 require
    a valid Setup PIN via `X-AstroDrive-Token` (hmac.compare_digest against
    /etc/onstep-autoprovision/webui.token).
  - Unauthenticated GET /api/status returns only minimal boolean state for the
    Astroberry overlay pill (no serials, paths, SSIDs, IPs, logs, or config).
  - `GITHUB_REPO` is read-only from the Web UI (cannot be repointed over HTTP).
  - Blank-board flashing requires explicit single-use authorization from the
    authenticated Setup Wizard unless the owner intentionally enabled AUTO_FLASH.
"""

from __future__ import annotations

import datetime as dt
import glob
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

WEBUI_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = WEBUI_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from onstep_common import (  # noqa: E402
    find_device_holders,
    get_udev_properties,
    parse_env_file,
    read_json_file,
    write_config_keys,
    write_json_atomic,
)

LOG = logging.getLogger("onstep-webui")
CONFIG_FILE = Path(os.environ.get("ONSTEP_CONFIG_FILE", "/etc/onstep-autoprovision/config.env"))
META_FILE = Path(os.environ.get("ONSTEP_META_FILE", "/etc/onstep-autoprovision/install-meta.env"))
TOKEN_FILE = Path(os.environ.get("ONSTEP_TOKEN_FILE", "/etc/onstep-autoprovision/webui.token"))
STATE_DIR = Path(os.environ.get("ONSTEP_STATE_DIR", "/var/lib/onstep-autoprovision"))
FIRMWARE_DIR = Path(os.environ.get("FIRMWARE_DIR", "/opt/onstep-autoprovision/firmware"))
SYMLINK_PATH = Path(os.environ.get("SYMLINK_PATH", "/dev/onstep"))
CAMERA_PREVIEW_FILE = Path(os.environ.get("CAMERA_PREVIEW_FILE", "/run/onstep-camera/preview.jpg"))
UPDATE_SCRIPT = Path(os.environ.get("UPDATE_SCRIPT", str(SCRIPTS_DIR / "update_firmware.sh")))
SOFTWARE_UPDATE_SCRIPT = Path(os.environ.get("SOFTWARE_UPDATE_SCRIPT", str(SCRIPTS_DIR / "update_software.sh")))
HOTSPOT_SCRIPT = Path(os.environ.get("HOTSPOT_SCRIPT", str(SCRIPTS_DIR / "setup_hotspot.sh")))
INDI_SCRIPT = Path(os.environ.get("INDI_SCRIPT", str(SCRIPTS_DIR / "configure_indi_defaults.sh")))

ALLOWED_SVG_FILES = {
    "wiring-esp32-tmc2208.svg",
    "wiring-nodemcu-esp8266-tmc2208.svg",
}

ALLOWED_CAMERA_RESOLUTIONS = {"320x240", "640x480", "800x600", "1280x720"}
ALLOWED_CAMERA_MODES = {"dynamic_drift", "fixed_ref"}
ALLOWED_CAMERA_ACTIONS = {
    "stack-reset": "stack_reset",
    "lock-auto": "lock_auto",
    "capture-dark": "capture_dark",
    "clear-dark": "clear_dark",
    "save": "save",
}
ALLOWED_V4L2_CONTROLS = {
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
CAPTURE_FILE_RE = re.compile(r"^astrodrive-[0-9]{8}-[0-9]{6}\.(png|json)$")

# M1: GITHUB_REPO is deliberately NOT mutable via HTTP!
ALLOWED_CONFIG_KEYS = {
    "TARGET_CHIP": re.compile(r"^(auto|esp8266|esp32)$"),
    "AUTO_FLASH": re.compile(r"^(true|false)$"),
    "AUTO_UPGRADE": re.compile(r"^(true|false)$"),
    "AUTO_COMPILE": re.compile(r"^(true|false)$"),
    "PREFER_LIVE_COMPILE": re.compile(r"^(true|false)$"),
    "ONLINE_CHECK_UPDATES": re.compile(r"^(true|false)$"),
    "AUTO_UPDATE_SOFTWARE": re.compile(r"^(true|false)$"),
    "CAMERA_ENABLED": re.compile(r"^(true|false)$"),
}

CONFIG_LOCK = threading.Lock()
STATUS_CACHE_LOCK = threading.Lock()
STATUS_CACHE_TTL_SEC = float(os.environ.get("WEBUI_STATUS_CACHE_TTL", "4.0"))
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
_EPHEMERAL_TOKEN: str | None = None


def invalidate_status_cache() -> None:
    global _STATUS_CACHE
    with STATUS_CACHE_LOCK:
        _STATUS_CACHE = None


def get_expected_token() -> tuple[str, bool]:
    """Return (token, token_file_error). Fails closed on missing/short token file."""
    global _EPHEMERAL_TOKEN
    try:
        if TOKEN_FILE.is_file():
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if len(tok) >= 16:
                return tok, False
    except OSError:
        pass
    if _EPHEMERAL_TOKEN is None:
        _EPHEMERAL_TOKEN = secrets.token_hex(16)
        LOG.warning("Token file %s missing or invalid (<16 chars); failing closed with ephemeral token.", TOKEN_FILE)
    return _EPHEMERAL_TOKEN, True


def read_config_env() -> dict[str, str]:
    values = {
        "TARGET_CHIP": "auto",
        "AUTO_FLASH": "false",
        "AUTO_UPGRADE": "false",
        "AUTO_COMPILE": "false",
        "PREFER_LIVE_COMPILE": "false",
        "ONLINE_CHECK_UPDATES": "true",
        "AUTO_UPDATE_SOFTWARE": "true",
        "CAMERA_ENABLED": "false",
        "GITHUB_REPO": "0denko/AstroDrive",
        "PROBE_BAUDS": "9600,115200,57600,19200",
        "ALLOWED_USB_SERIALS": "",
        "ALLOWED_USB_PATHS": "",
    }
    parsed = parse_env_file(CONFIG_FILE)
    for k in values:
        if k in parsed:
            values[k] = parsed[k]
    return values


def update_config_env(updates: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, val in updates.items():
        if key not in ALLOWED_CONFIG_KEYS:
            raise ValueError(f"Unsupported or read-only config key: {key}")
        sval = str(val).strip()
        if not ALLOWED_CONFIG_KEYS[key].match(sval):
            raise ValueError(f"Invalid value for {key}: {sval!r}")
        clean[key] = sval
    with CONFIG_LOCK:
        write_config_keys(CONFIG_FILE, clean)
    if "CAMERA_ENABLED" in clean:
        action = "restart" if clean["CAMERA_ENABLED"] == "true" else "stop"
        try:
            subprocess.Popen(
                ["systemctl", action, "--no-block", "onstep-camera.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    invalidate_status_cache()
    return read_config_env()


def remember_usb_device(device: str) -> str:
    """Record a specific board's USB serial or physical USB path into config.env allowlist."""
    props = get_udev_properties(device)
    serial_short = props.get("ID_SERIAL_SHORT", "").strip()
    id_path = props.get("ID_PATH", "").strip()
    cfg = read_config_env()

    if serial_short and serial_short.lower() not in {"0001", "0000", "1234"}:
        cur = [x.strip() for x in cfg.get("ALLOWED_USB_SERIALS", "").split(",") if x.strip()]
        if serial_short not in cur:
            cur.append(serial_short)
            with CONFIG_LOCK:
                write_config_keys(CONFIG_FILE, {"ALLOWED_USB_SERIALS": ",".join(cur)})
            invalidate_status_cache()
        return f"USB serial {serial_short}"
    if id_path:
        cur = [x.strip() for x in cfg.get("ALLOWED_USB_PATHS", "").split(",") if x.strip()]
        if id_path not in cur:
            cur.append(id_path)
            with CONFIG_LOCK:
                write_config_keys(CONFIG_FILE, {"ALLOWED_USB_PATHS": ",".join(cur)})
            invalidate_status_cache()
        return f"USB port {id_path}"
    return ""


def get_provisioning_units() -> list[str]:
    try:
        res = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=activating,running", "--no-legend", "onstep-autoprovision@*"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        return [line.split()[0] for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def list_cached_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    if not FIRMWARE_DIR.is_dir():
        return profiles
    for manifest_path in sorted(FIRMWARE_DIR.glob("*/manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            bin_name = data.get("binary", "")
            data["binary_present"] = bool(bin_name and (manifest_path.parent / bin_name).is_file())
            profiles.append(data)
        except Exception as exc:
            profiles.append({"profile": manifest_path.parent.name, "error": str(exc)})
    return profiles


def list_usb_serial_devices() -> list[dict[str, str]]:
    """Return all attached ttyUSB* and ttyACM* devices with udev match metadata (S3)."""
    matched_targets: set[str] = set()
    for link in glob.glob("/dev/onstep-raw-*"):
        try:
            matched_targets.add(os.path.realpath(link))
        except OSError:
            continue

    devs: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        props = get_udev_properties(real)
        vid = props.get("ID_VENDOR_ID", "")
        pid = props.get("ID_MODEL_ID", "")
        devs.append({
            "path": real,
            "name": os.path.basename(real),
            "vid_pid": f"{vid}:{pid}" if vid and pid else "",
            "model": props.get("ID_MODEL_FROM_DATABASE") or props.get("ID_MODEL") or "",
            "serial": props.get("ID_SERIAL_SHORT", ""),
            "usb_path": props.get("ID_PATH", ""),
            "udev_matched": "true" if real in matched_targets else "false",
        })
    return devs


def resolve_active_tty() -> tuple[str | None, bool]:
    if SYMLINK_PATH.exists():
        return os.path.realpath(str(SYMLINK_PATH)), True
    raw_links = sorted(glob.glob("/dev/onstep-raw-*"))
    if raw_links:
        return os.path.realpath(raw_links[0]), False
    ttys = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    return (ttys[0], False) if ttys else (None, False)


def probe_if_safe(device: str | None, bauds_csv: str) -> dict[str, Any]:
    if not device or not os.path.exists(device):
        return {"state": "disconnected"}
    if get_provisioning_units():
        return {"state": "provisioning"}
    holders = find_device_holders(device)
    if holders:
        return {
            "state": "in_use",
            "holders": [{"pid": p, "comm": c} for p, c in holders],
        }
    try:
        from probe_onstep import probe_port
    except ImportError:
        return {"state": "error", "error": "pyserial not installed (run inside /opt/onstep-autoprovision/.venv)"}
    bauds = [int(b.strip()) for b in bauds_csv.split(",") if b.strip().isdigit()] or [9600]
    for baud in bauds:
        try:
            info = probe_port(device, baud, timeout=1.0)
            if info:
                return {"state": "online", **info}
        except Exception:
            continue
    return {"state": "unresponsive"}


def read_pi_health() -> dict[str, Any]:
    throttled_hex: str | None = None
    undervoltage_now = False
    undervoltage_past = False

    vcgencmd = shutil.which("vcgencmd")
    if vcgencmd:
        try:
            res = subprocess.run([vcgencmd, "get_throttled"], capture_output=True, text=True, timeout=2)
            m = re.search(r"throttled=(0x[0-9a-fA-F]+)", res.stdout)
            if m:
                throttled_hex = m.group(1).lower()
                val = int(throttled_hex, 16)
                undervoltage_now = bool(val & 0x1)
                undervoltage_past = bool(val & 0x10000)
        except Exception:
            pass

    indi_configured = False
    for home_dir in glob.glob("/home/*"):
        cfg = Path(home_dir) / ".indi" / "LX200 OnStep_config.xml"
        try:
            if cfg.is_file() and "/dev/onstep" in cfg.read_text(encoding="utf-8", errors="replace"):
                indi_configured = True
                break
        except OSError:
            continue

    return {
        "hostname": socket.gethostname(),
        "system_time_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "throttled_hex": throttled_hex,
        "undervoltage_now": undervoltage_now,
        "undervoltage_past": undervoltage_past,
        "indi_configured": indi_configured,
        "arduino_cli_installed": bool(shutil.which("arduino-cli")),
    }


def read_network_status() -> dict[str, Any]:
    nmcli = shutil.which("nmcli")
    active_wifi = ""
    hotspot_configured = False
    hotspot_ssid = ""
    home_wifi_configured = False
    home_wifi_ssid = ""
    ip_addresses: list[str] = []

    try:
        res = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            ip_addresses = [ip for ip in res.stdout.strip().split() if "." in ip]
    except Exception:
        pass

    if nmcli:
        try:
            res = subprocess.run(
                [nmcli, "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in res.stdout.splitlines():
                if line.startswith("yes:"):
                    active_wifi = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass

        for con_name in ("astropi-hotspot", "home-wifi"):
            try:
                res = subprocess.run(
                    [nmcli, "-g", "802-11-wireless.ssid", "connection", "show", con_name],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if res.returncode == 0 and res.stdout.strip():
                    if con_name == "astropi-hotspot":
                        hotspot_configured = True
                        hotspot_ssid = res.stdout.strip()
                    else:
                        home_wifi_configured = True
                        home_wifi_ssid = res.stdout.strip()
            except Exception:
                pass

    return {
        "nmcli_available": bool(nmcli),
        "active_wifi": active_wifi,
        "hotspot_configured": hotspot_configured,
        "hotspot_ssid": hotspot_ssid,
        "home_wifi_configured": home_wifi_configured,
        "home_wifi_ssid": home_wifi_ssid,
        "ip_addresses": ip_addresses,
    }


def read_camera_status(cfg: dict[str, str]) -> dict[str, Any]:
    """Read camera daemon status & saved captures summary (authenticated status only)."""
    enabled = cfg.get("CAMERA_ENABLED", "false") == "true"
    st = read_json_file(STATE_DIR / "camera-status.json")
    if not st:
        st = {
            "enabled": enabled,
            "state": "starting" if enabled else "disabled",
            "message": (
                "Camera service is starting…"
                if enabled
                else "Camera service is disabled. Toggle Enable Camera Service below to start it."
            ),
        }
    st["enabled"] = enabled
    st["preview_ready"] = CAMERA_PREVIEW_FILE.is_file()
    captures_dir = STATE_DIR / "captures"
    st["captures_count"] = len(list(captures_dir.glob("astrodrive-*.png"))) if captures_dir.is_dir() else 0
    return st


def list_camera_captures(limit: int = 30) -> list[dict[str, Any]]:
    captures_dir = STATE_DIR / "captures"
    if not captures_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for png in sorted(captures_dir.glob("astrodrive-*.png"), reverse=True)[:limit]:
        if not CAPTURE_FILE_RE.match(png.name):
            continue
        meta = read_json_file(png.with_suffix(".json"))
        try:
            size_kb = round(png.stat().st_size / 1024.0, 1)
        except OSError:
            size_kb = 0.0
        items.append({
            "file": png.name,
            "meta_file": png.with_suffix(".json").name if png.with_suffix(".json").is_file() else "",
            "size_kb": size_kb,
            "saved_at_utc": meta.get("saved_at_utc", ""),
            "resolution": meta.get("resolution", ""),
            "metrics": meta.get("metrics", {}),
        })
    return items


def update_camera_config(body: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist camera stacker & V4L2 settings for onstep_camera.py."""
    cmd_dir = STATE_DIR / "camera-cmd"
    cfg_path = cmd_dir / "config.json"
    merged = read_json_file(cfg_path)
    merged.pop("v4l2_controls", None)

    if "stacking" in body:
        if not isinstance(body["stacking"], bool):
            raise ValueError("stacking must be a boolean")
        merged["stacking"] = body["stacking"]

    if "mode" in body:
        mode = str(body["mode"]).strip()
        if mode not in ALLOWED_CAMERA_MODES:
            raise ValueError(f"Unsupported mode: {mode!r}")
        merged["mode"] = mode

    if "keep_pct" in body:
        kp = int(body["keep_pct"])
        if kp < 10 or kp > 100:
            raise ValueError("keep_pct must be between 10 and 100")
        merged["keep_pct"] = kp

    if "resolution" in body:
        res = str(body["resolution"]).strip()
        if res not in ALLOWED_CAMERA_RESOLUTIONS:
            raise ValueError(f"Unsupported resolution: {res!r}")
        merged["resolution"] = res

    if "stretch_asinh" in body:
        sa = float(body["stretch_asinh"])
        if not (0.0 <= sa <= 20.0):
            raise ValueError("stretch_asinh must be between 0.0 and 20.0")
        merged["stretch_asinh"] = round(sa, 3)

    if "black_point" in body:
        bp = float(body["black_point"])
        if not (0.0 <= bp <= 0.8):
            raise ValueError("black_point must be between 0.0 and 0.8")
        merged["black_point"] = round(bp, 4)

    if "white_point" in body:
        wp = float(body["white_point"])
        if not (0.1 <= wp <= 1.0):
            raise ValueError("white_point must be between 0.1 and 1.0")
        merged["white_point"] = round(wp, 4)

    bp_cur = float(merged.get("black_point", 0.02))
    wp_cur = float(merged.get("white_point", 0.98))
    if wp_cur <= bp_cur + 0.05:
        raise ValueError("white_point must be at least 0.05 above black_point")

    if "v4l2_controls" in body:
        raw_ctrls = body["v4l2_controls"]
        if not isinstance(raw_ctrls, dict):
            raise ValueError("v4l2_controls must be an object")
        clean_ctrls: dict[str, int] = {}
        for k, v in raw_ctrls.items():
            if k not in ALLOWED_V4L2_CONTROLS:
                raise ValueError(f"Unsupported V4L2 control: {k!r}")
            ival = int(v)
            if ival < -65536 or ival > 1000000:
                raise ValueError(f"Out-of-range value for {k}: {ival}")
            clean_ctrls[k] = ival
        if clean_ctrls:
            merged["v4l2_controls"] = clean_ctrls

    cmd_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(cfg_path, merged)
    invalidate_status_cache()
    return merged


def queue_camera_action(action_slug: str, body: dict[str, Any]) -> dict[str, Any]:
    mapped = ALLOWED_CAMERA_ACTIONS.get(action_slug)
    if not mapped:
        raise ValueError(f"Unsupported camera action: {action_slug!r}")
    payload: dict[str, Any] = {"action": mapped}
    if mapped == "capture_dark" and "count" in body:
        payload["count"] = max(3, min(int(body["count"]), 50))
    cmd_dir = STATE_DIR / "camera-cmd"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    action_path = cmd_dir / f"action-{time.time_ns()}.json"
    write_json_atomic(action_path, payload)
    invalidate_status_cache()
    return payload


def _build_full_status_uncached(run_probe: bool = False) -> dict[str, Any]:
    cfg = read_config_env()
    meta = parse_env_file(META_FILE)
    tty, symlink_ready = resolve_active_tty()
    active_units = get_provisioning_units()
    holders = find_device_holders(tty) if tty and os.path.exists(tty) else []
    indi_active = any("indi_lx200" in c or "indiserver" in c for _, c in holders)

    probe_info: dict[str, Any]
    if active_units:
        probe_info = {"state": "provisioning"}
    elif indi_active:
        probe_info = {"state": "indi_connected"}
    elif run_probe:
        probe_info = probe_if_safe(tty, cfg.get("PROBE_BAUDS", "9600"))
    elif symlink_ready:
        probe_info = {"state": "ready"}
    elif tty:
        probe_info = {"state": "detected_unverified"}
    else:
        probe_info = {"state": "disconnected"}

    install_status = read_json_file(STATE_DIR / "install-status.json")
    overlay_status = read_json_file(STATE_DIR / "overlay-status.json")
    if overlay_status:
        install_status["nginx_overlay"] = overlay_status.get("status", install_status.get("nginx_overlay", ""))
        install_status["nginx_overlay_detail"] = overlay_status.get("detail", "")

    software_update = read_json_file(STATE_DIR / "software-update.json")
    if meta:
        software_update.setdefault("installed_version", meta.get("INSTALLED_VERSION", ""))
        software_update.setdefault("installed_commit", meta.get("INSTALLED_COMMIT", ""))

    _, token_err = get_expected_token()
    unrestricted_auto_flash = (
        cfg.get("AUTO_FLASH") == "true"
        and not cfg.get("ALLOWED_USB_SERIALS", "").strip()
        and not cfg.get("ALLOWED_USB_PATHS", "").strip()
    )

    return {
        "auth_required": True,
        "authenticated": True,
        "token_file_error": token_err,
        "symlink_path": str(SYMLINK_PATH),
        "symlink_ready": symlink_ready,
        "device": tty,
        "device_present": bool(tty),
        "usb_devices": list_usb_serial_devices(),
        "provisioning_active": bool(active_units),
        "provisioning_units": active_units,
        "last_provision": read_json_file(STATE_DIR / "provision-status.json"),
        "indi_active": indi_active,
        "holders": [{"pid": p, "comm": c} for p, c in holders],
        "probe": probe_info,
        "cached_profiles": list_cached_profiles(),
        "auto_flash_unrestricted_warning": unrestricted_auto_flash,
        "config": {
            **{k: cfg[k] for k in ALLOWED_CONFIG_KEYS if k in cfg},
            "GITHUB_REPO": cfg.get("GITHUB_REPO", ""),
            "ALLOWED_USB_SERIALS": cfg.get("ALLOWED_USB_SERIALS", ""),
            "ALLOWED_USB_PATHS": cfg.get("ALLOWED_USB_PATHS", ""),
        },
        "health": read_pi_health(),
        "network": read_network_status(),
        "install_status": install_status,
        "software_update": software_update,
        "camera": read_camera_status(cfg),
    }


def build_status(run_probe: bool = False, authenticated: bool = True) -> dict[str, Any]:
    """Return status with a short TTL cache (#1) and redacted payload when unauthenticated (M1 #1)."""
    global _STATUS_CACHE
    if run_probe:
        full = _build_full_status_uncached(run_probe=True)
    else:
        now = time.monotonic()
        with STATUS_CACHE_LOCK:
            if _STATUS_CACHE and (now - _STATUS_CACHE[0]) < STATUS_CACHE_TTL_SEC:
                full = _STATUS_CACHE[1]
            else:
                full = _build_full_status_uncached(run_probe=False)
                _STATUS_CACHE = (now, full)

    if authenticated:
        return full

    # M1 residual #1: Unauthenticated callers get ONLY minimal pill state (no serials/IPs/SSIDs/config/camera)
    return {
        "auth_required": True,
        "authenticated": False,
        "token_file_error": full.get("token_file_error", False),
        "symlink_ready": full.get("symlink_ready", False),
        "device_present": full.get("device_present", False),
        "provisioning_active": full.get("provisioning_active", False),
        "indi_active": full.get("indi_active", False),
    }


def read_logs(lines: int = 120) -> str:
    lines = max(10, min(lines, 500))
    try:
        res = subprocess.run(
            [
                "journalctl",
                "-u",
                "onstep-autoprovision@*",
                "-u",
                "onstep-firmware-update.service",
                "-u",
                "astrodrive-update.service",
                "-u",
                "onstep-camera.service",
                "--no-pager",
                "-n",
                str(lines),
                "-o",
                "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.stdout or "(No provisioning logs yet — plug in an ESP32 or NodeMCU board.)"
    except Exception as exc:
        return f"[ERROR] Unable to read journalctl logs: {exc}"


def _clean_wifi_str(val: Any, max_len: int = 64) -> str:
    s = str(val or "").strip()
    if len(s) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise ValueError("Invalid characters or length in Wi-Fi field.")
    return s


class OnStepWebUIHandler(BaseHTTPRequestHandler):
    server_version = "OnStepOverlayWebUI/2.3"

    def log_message(self, fmt: str, *args: Any) -> None:
        # #1: Suppress noisy INFO logs on high-frequency read-only polling routes to reduce SD wear
        path = urlparse(getattr(self, "path", "")).path
        if path in {
            "/api/status",
            "/onstep/api/status",
            "/api/logs",
            "/onstep/api/logs",
            "/api/camera/preview.jpg",
            "/onstep/api/camera/preview.jpg",
            "/api/camera/captures",
            "/onstep/api/camera/captures",
        }:
            return
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _normalized_path(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/onstep":
            path = "/"
        elif path.startswith("/onstep/"):
            path = path[len("/onstep") :]
        return path, parse_qs(parsed.query)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json({"error": f"Asset missing: {path.name}"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 32768:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _is_authenticated(self) -> bool:
        provided = (self.headers.get("X-AstroDrive-Token") or "").strip()
        expected, _ = get_expected_token()
        return bool(provided) and hmac.compare_digest(provided, expected)

    def _require_auth(self) -> bool:
        if self._is_authenticated():
            return True
        _, token_err = get_expected_token()
        msg = (
            "Setup token file (/etc/onstep-autoprovision/webui.token) is missing or invalid. "
            "Re-run `sudo /opt/astrodrive-src/install.sh` on the Pi to regenerate it."
            if token_err
            else "Setup PIN required. Enter the PIN printed by setup.sh (or stored in /etc/onstep-autoprovision/webui.token)."
        )
        self._send_json(
            {"ok": False, "auth_error": True, "token_file_error": token_err, "error": msg},
            HTTPStatus.UNAUTHORIZED,
        )
        return False

    def do_GET(self) -> None:
        path, qs = self._normalized_path()
        if path in {"/", "/index.html"}:
            self._send_file(WEBUI_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/overlay.js":
            self._send_file(WEBUI_DIR / "overlay.js", "application/javascript; charset=utf-8")
        elif path.startswith("/docs/"):
            fname = path[len("/docs/") :]
            if fname in ALLOWED_SVG_FILES:
                candidate = WEBUI_DIR / "docs" / fname
                if not candidate.is_file():
                    candidate = WEBUI_DIR.parent / "docs" / fname
                self._send_file(candidate, "image/svg+xml; charset=utf-8")
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        elif path == "/api/status":
            do_probe = qs.get("probe", ["0"])[0] in {"1", "true"}
            if do_probe and not self._require_auth():
                return
            self._send_json(build_status(run_probe=do_probe, authenticated=self._is_authenticated()))
        elif path == "/api/logs":
            if not self._require_auth():
                return
            try:
                lines = int(qs.get("lines", ["120"])[0])
            except ValueError:
                lines = 120
            self._send_json({"logs": read_logs(lines)})
        elif path == "/api/camera/preview.jpg":
            if not self._require_auth():
                return
            if not CAMERA_PREVIEW_FILE.is_file():
                self._send_json({"ok": False, "error": "Camera preview not ready"}, HTTPStatus.NOT_FOUND)
                return
            try:
                body = CAMERA_PREVIEW_FILE.read_bytes()
            except OSError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/camera/captures":
            if not self._require_auth():
                return
            self._send_json({"ok": True, "captures": list_camera_captures()})
        elif path.startswith("/api/camera/captures/"):
            if not self._require_auth():
                return
            fname = path[len("/api/camera/captures/") :]
            if not CAPTURE_FILE_RE.match(fname):
                self._send_json({"ok": False, "error": "Invalid capture filename"}, HTTPStatus.BAD_REQUEST)
                return
            target = (STATE_DIR / "captures" / fname).resolve()
            captures_root = (STATE_DIR / "captures").resolve()
            if captures_root not in target.parents or not target.is_file():
                self._send_json({"ok": False, "error": "Capture not found"}, HTTPStatus.NOT_FOUND)
                return
            ctype = "image/png" if fname.endswith(".png") else "application/json; charset=utf-8"
            self._send_file(target, ctype)
        else:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path, _ = self._normalized_path()
        if not self._require_auth():
            return

        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Invalid JSON: {exc}"}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/auth/verify":
            self._send_json({"ok": True, "message": "Setup PIN verified."})
            return

        if path == "/api/config":
            try:
                updated = update_config_env(body)
                self._send_json({"ok": True, "config": updated})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/probe":
            invalidate_status_cache()
            self._send_json(build_status(run_probe=True, authenticated=True))
            return

        if path == "/api/action/update-cache":
            if not UPDATE_SCRIPT.is_file():
                self._send_json({"ok": False, "error": "update_firmware.sh not installed"}, HTTPStatus.NOT_FOUND)
                return
            try:
                subprocess.Popen(
                    ["systemctl", "start", "--no-block", "onstep-firmware-update.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                invalidate_status_cache()
                self._send_json({"ok": True, "message": "Downloading latest firmware from GitHub Releases…"})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/update-software":
            if not SOFTWARE_UPDATE_SCRIPT.is_file():
                self._send_json({"ok": False, "error": "update_software.sh not installed"}, HTTPStatus.NOT_FOUND)
                return
            try:
                subprocess.Popen(
                    ["systemctl", "start", "--no-block", "astrodrive-update.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                invalidate_status_cache()
                self._send_json({"ok": True, "message": "Checking GitHub for AstroDrive software updates…"})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/provision":
            requested_dev = str(body.get("device") or "").strip()
            if requested_dev:
                if not re.match(r"^/dev/tty(USB|ACM)[0-9]+$", requested_dev) or not os.path.exists(requested_dev):
                    self._send_json({"ok": False, "error": f"USB serial device not found: {requested_dev}"}, HTTPStatus.BAD_REQUEST)
                    return
                tty = os.path.realpath(requested_dev)
            else:
                tty, _ = resolve_active_tty()
                if not tty:
                    self._send_json({"ok": False, "error": "No ESP32 or NodeMCU USB board detected. Plug it in first."}, HTTPStatus.NOT_FOUND)
                    return

            profiles = list_cached_profiles()
            has_fw = any(p.get("binary_present") for p in profiles)
            if not has_fw and not shutil.which("arduino-cli"):
                self._send_json(
                    {
                        "ok": False,
                        "error": "No firmware is cached on this Pi yet. Connect to the internet and click 'Download Firmware' in Step 1 first.",
                    },
                    HTTPStatus.PRECONDITION_FAILED,
                )
                return

            holders = find_device_holders(tty)
            if any("indi_lx200" in c or "indiserver" in c for _, c in holders):
                self._send_json(
                    {
                        "ok": False,
                        "error": "KStars/Ekos (INDI) is currently connected to the mount. Disconnect the mount first.",
                    },
                    HTTPStatus.CONFLICT,
                )
                return

            remembered = ""
            if bool(body.get("remember_usb", True)):
                remembered = remember_usb_device(tty)

            dev_name = os.path.basename(tty)
            if bool(body.get("authorize_flash", True)):
                ticket_dir = STATE_DIR / "flash-once"
                ticket_dir.mkdir(parents=True, exist_ok=True)
                ticket = ticket_dir / dev_name
                ticket.write_text(f"authorized_at={dt.datetime.now(dt.timezone.utc).isoformat()}\n", encoding="utf-8")
                os.chmod(ticket, 0o600)

            try:
                subprocess.Popen(
                    ["systemctl", "start", "--no-block", f"onstep-autoprovision@{dev_name}.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                invalidate_status_cache()
                note = f" (saved {remembered} to allowlist)" if remembered else ""
                self._send_json({
                    "ok": True,
                    "unit": f"onstep-autoprovision@{dev_name}.service",
                    "message": f"Flashing started on {tty}{note}!",
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/setup-hotspot":
            if not HOTSPOT_SCRIPT.is_file():
                self._send_json({"ok": False, "error": "setup_hotspot.sh not installed"}, HTTPStatus.NOT_FOUND)
                return
            try:
                ap_ssid = _clean_wifi_str(body.get("ap_ssid") or "AstroPi-OnStep", 32)
                ap_pass = _clean_wifi_str(body.get("ap_pass") or "", 63)
                home_ssid = _clean_wifi_str(body.get("home_ssid") or "", 32)
                home_pass = _clean_wifi_str(body.get("home_pass") or "", 63)
                if not ap_ssid:
                    raise ValueError("Field Hotspot Name cannot be empty.")
                if len(ap_pass) < 8:
                    raise ValueError("Field Hotspot Password must be at least 8 characters.")
                if home_ssid and len(home_pass) < 8:
                    raise ValueError("Home Wi-Fi Password must be at least 8 characters.")

                cmd = [str(HOTSPOT_SCRIPT), "--ap-ssid", ap_ssid, "--ap-pass", ap_pass]
                if home_ssid:
                    cmd.extend(["--home-ssid", home_ssid, "--home-pass", home_pass])

                res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                if res.returncode != 0:
                    err_msg = (res.stderr or res.stdout or "Failed to configure Wi-Fi/Hotspot").strip()
                    self._send_json({"ok": False, "error": err_msg}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                invalidate_status_cache()
                msg = f"Saved! Field hotspot '{ap_ssid}' (10.42.0.1) is ready."
                if home_ssid:
                    msg = f"Saved! Home Wi-Fi '{home_ssid}' + field hotspot '{ap_ssid}' (10.42.0.1) are ready."
                self._send_json({"ok": True, "message": msg})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/configure-indi":
            if not INDI_SCRIPT.is_file():
                self._send_json({"ok": False, "error": "configure_indi_defaults.sh not installed"}, HTTPStatus.NOT_FOUND)
                return
            try:
                res = subprocess.run([str(INDI_SCRIPT), "--force"], capture_output=True, text=True, timeout=10)
                if res.returncode != 0:
                    self._send_json(
                        {"ok": False, "error": (res.stderr or res.stdout or "Failed to apply INDI defaults").strip()},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                invalidate_status_cache()
                self._send_json({"ok": True, "message": "INDI LX200 OnStep defaults saved (/dev/onstep @ 9600 baud)."})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/sync-time":
            try:
                epoch_ms = int(body.get("epoch_ms", 0))
                epoch_sec = epoch_ms // 1000
                if epoch_sec < 1704067200 or epoch_sec > 2208988800:
                    raise ValueError("Invalid timestamp received from browser.")
                res = subprocess.run(["date", "-u", "-s", f"@{epoch_sec}"], capture_output=True, text=True, timeout=5)
                if res.returncode != 0:
                    self._send_json(
                        {"ok": False, "error": (res.stderr or "Unable to set system clock").strip()},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                invalidate_status_cache()
                now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                self._send_json({"ok": True, "message": f"Pi clock synced to {now_str}", "system_time_utc": now_str})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/camera/config":
            try:
                updated = update_camera_config(body)
                self._send_json({"ok": True, "settings": updated, "message": "Camera settings applied."})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path.startswith("/api/camera/action/"):
            slug = path[len("/api/camera/action/") :]
            try:
                queued = queue_camera_action(slug, body)
                labels = {
                    "stack_reset": "Stack reset & recentred.",
                    "lock_auto": "Requested UVC auto-control lock (manual exposure/gain/WB/focus).",
                    "capture_dark": f"Capturing {queued.get('count', 15)} dark frames (cover telescope/lens cap!)…",
                    "clear_dark": "Cleared master dark frame.",
                    "save": "Saving stretched PNG + metadata to /var/lib/onstep-autoprovision/captures…",
                }
                self._send_json({"ok": True, "action": queued["action"], "message": labels.get(queued["action"], "Action queued.")})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBUI_PORT", "8090"))
    httpd = ThreadingHTTPServer((host, port), OnStepWebUIHandler)
    LOG.info("AstroDrive Web UI & Setup Wizard listening on http://%s:%d/onstep/", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
