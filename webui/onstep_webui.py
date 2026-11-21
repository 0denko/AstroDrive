#!/usr/bin/env python3
"""
Lightweight, zero-dependency Web UI, Setup Wizard & REST API for AstroDrive.

Runs on port 8090 (exposed cleanly inside Astroberry OS at http://astroberry.local/onstep/
via Nginx overlay, and also accessible standalone at http://<pi-ip>:8090/).

Endpoints (served under both `/` and `/onstep/`):
  GET  /                         -> Guided Setup Wizard + OnStepX Dashboard (index.html)
  GET  /overlay.js               -> Astroberry Manager floating button & drawer script
  GET  /docs/<wiring>.svg        -> Offline wiring diagrams for ESP32 & NodeMCU ESP8266
  GET  /api/status               -> Live USB, /dev/onstep, INDI lock, Pi power health, Wi-Fi & config
  GET  /api/logs?lines=120       -> Live journalctl output for provisioning & firmware updates
  POST /api/config               -> Update allowlisted settings in /etc/onstep-autoprovision/config.env
  POST /api/action/probe         -> Non-invasive LX200 :GVP#/:GVN# probe (aborts if INDI active)
  POST /api/action/update-cache  -> Background fetch of latest GitHub Release firmware
  POST /api/action/provision     -> Trigger systemd onstep-autoprovision@<tty>.service (INDI-safe)
  POST /api/action/setup-hotspot -> Configure Home Wi-Fi + Dark-Sky Hotspot via setup_hotspot.sh
  POST /api/action/configure-indi-> Write INDI LX200 OnStep defaults (/dev/onstep @ 9600 baud)
  POST /api/action/sync-time     -> Sync Raspberry Pi system clock from phone/laptop browser
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

WEBUI_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = WEBUI_DIR.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

LOG = logging.getLogger("onstep-webui")
CONFIG_FILE = Path(os.environ.get("ONSTEP_CONFIG_FILE", "/etc/onstep-autoprovision/config.env"))
FIRMWARE_DIR = Path(os.environ.get("FIRMWARE_DIR", "/opt/onstep-autoprovision/firmware"))
SYMLINK_PATH = Path(os.environ.get("SYMLINK_PATH", "/dev/onstep"))
RAW_SYMLINK_PATH = Path("/dev/onstep-raw")
UPDATE_SCRIPT = Path(os.environ.get("UPDATE_SCRIPT", str(SCRIPTS_DIR / "update_firmware.sh")))
HOTSPOT_SCRIPT = Path(os.environ.get("HOTSPOT_SCRIPT", str(SCRIPTS_DIR / "setup_hotspot.sh")))
INDI_SCRIPT = Path(os.environ.get("INDI_SCRIPT", str(SCRIPTS_DIR / "configure_indi_defaults.sh")))

ALLOWED_SVG_FILES = {
    "wiring-esp32-tmc2208.svg",
    "wiring-nodemcu-esp8266-tmc2208.svg",
}

ALLOWED_CONFIG_KEYS = {
    "TARGET_CHIP": re.compile(r"^(auto|esp8266|esp32)$"),
    "AUTO_FLASH": re.compile(r"^(true|false)$"),
    "AUTO_UPGRADE": re.compile(r"^(true|false)$"),
    "AUTO_COMPILE": re.compile(r"^(true|false)$"),
    "PREFER_LIVE_COMPILE": re.compile(r"^(true|false)$"),
    "ONLINE_CHECK_UPDATES": re.compile(r"^(true|false)$"),
    "GITHUB_REPO": re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$"),
}

CONFIG_LOCK = threading.Lock()


def read_config_env() -> dict[str, str]:
    values = {
        "TARGET_CHIP": "auto",
        "AUTO_FLASH": "false",
        "AUTO_UPGRADE": "false",
        "AUTO_COMPILE": "true",
        "PREFER_LIVE_COMPILE": "false",
        "ONLINE_CHECK_UPDATES": "true",
        "GITHUB_REPO": "",
        "PROBE_BAUDS": "9600,115200,57600,19200",
    }
    if not CONFIG_FILE.is_file():
        return values
    for line in CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k in values:
            values[k] = v
    return values


def update_config_env(updates: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, val in updates.items():
        if key not in ALLOWED_CONFIG_KEYS:
            raise ValueError(f"Unsupported config key: {key}")
        sval = str(val).strip()
        if not ALLOWED_CONFIG_KEYS[key].match(sval):
            raise ValueError(f"Invalid value for {key}: {sval!r}")
        clean[key] = sval

    with CONFIG_LOCK:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines() if CONFIG_FILE.is_file() else []
        seen: set[str] = set()
        out_lines: list[str] = []
        for line in lines:
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
            if m and m.group(1) in clean:
                key = m.group(1)
                out_lines.append(f'{key}="{clean[key]}"')
                seen.add(key)
            else:
                out_lines.append(line)
        for key, sval in clean.items():
            if key not in seen:
                out_lines.append(f'{key}="{sval}"')
        CONFIG_FILE.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    return read_config_env()


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


def resolve_active_tty() -> tuple[str | None, bool]:
    if SYMLINK_PATH.exists():
        return os.path.realpath(str(SYMLINK_PATH)), True
    if RAW_SYMLINK_PATH.exists():
        return os.path.realpath(str(RAW_SYMLINK_PATH)), False
    ttys = sorted(glob.glob("/dev/ttyUSB*"))
    return (ttys[0], False) if ttys else (None, False)


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


def build_status(run_probe: bool = False) -> dict[str, Any]:
    cfg = read_config_env()
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

    return {
        "symlink_path": str(SYMLINK_PATH),
        "symlink_ready": symlink_ready,
        "device": tty,
        "usb_devices": sorted(glob.glob("/dev/ttyUSB*")),
        "provisioning_active": bool(active_units),
        "provisioning_units": active_units,
        "indi_active": indi_active,
        "holders": [{"pid": p, "comm": c} for p, c in holders],
        "probe": probe_info,
        "cached_profiles": list_cached_profiles(),
        "config": {k: cfg[k] for k in ALLOWED_CONFIG_KEYS if k in cfg},
        "health": read_pi_health(),
        "network": read_network_status(),
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
    server_version = "OnStepOverlayWebUI/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
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
            self._send_json(build_status(run_probe=do_probe))
        elif path == "/api/logs":
            try:
                lines = int(qs.get("lines", ["120"])[0])
            except ValueError:
                lines = 120
            self._send_json({"logs": read_logs(lines)})
        else:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path, _ = self._normalized_path()
        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"error": f"Invalid JSON: {exc}"}, HTTPStatus.BAD_REQUEST)
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
            self._send_json(build_status(run_probe=True))
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
                self._send_json({"ok": True, "message": "Downloading latest firmware from GitHub Releases…"})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/provision":
            tty, _ = resolve_active_tty()
            if not tty:
                self._send_json({"ok": False, "error": "No ESP32 or NodeMCU USB board detected. Plug it in first."}, HTTPStatus.NOT_FOUND)
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
            dev_name = os.path.basename(tty)
            try:
                subprocess.Popen(
                    ["systemctl", "start", "--no-block", f"onstep-autoprovision@{dev_name}.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._send_json({"ok": True, "unit": f"onstep-autoprovision@{dev_name}.service"})
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
                self._send_json({"ok": True, "message": "INDI LX200 OnStep defaults saved (/dev/onstep @ 9600 baud)."})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/action/sync-time":
            try:
                epoch_ms = int(body.get("epoch_ms", 0))
                epoch_sec = epoch_ms // 1000
                # Sanity check: 2024-01-01 to 2040-01-01
                if epoch_sec < 1704067200 or epoch_sec > 2208988800:
                    raise ValueError("Invalid timestamp received from browser.")
                res = subprocess.run(["date", "-u", "-s", f"@{epoch_sec}"], capture_output=True, text=True, timeout=5)
                if res.returncode != 0:
                    self._send_json(
                        {"ok": False, "error": (res.stderr or "Unable to set system clock").strip()},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                self._send_json({"ok": True, "message": f"Pi clock synced to {now_str}", "system_time_utc": now_str})
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
