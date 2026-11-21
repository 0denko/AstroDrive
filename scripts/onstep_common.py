#!/usr/bin/env python3
"""
Shared helpers for AstroDrive scripts and Web UI (single source of truth for
/proc/*/fd inspection, udev property lookup, atomic JSON state writes, and
safe config.env key parsing/updates).

CLI subcommands so bash scripts (install.sh, update_software.sh, update_firmware.sh)
never `source` config.env or install-meta.env as root:
  python3 onstep_common.py indi-active
  python3 onstep_common.py get-config <file> <KEY> [default]
  python3 onstep_common.py set-config <file> <KEY> <VALUE>
  python3 onstep_common.py write-json <file> '<json-object>'
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

USB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
CONFIG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_safe_usb_id(value: str) -> bool:
    """Return True iff `value` is a safe USB serial or udev ID_PATH token."""
    return bool(USB_ID_RE.match(value.strip()))


def _escape_env_value(value: str) -> str:
    s = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise ValueError("Config value must not contain control characters or newlines.")
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def _unescape_env_value(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
        s = (
            s.replace("\\`", "`")
            .replace("\\$", "$")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
        return s
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1]
    return s


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically using a PID+TID-unique temp file and fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if CONFIG_KEY_RE.match(key):
            out[key] = _unescape_env_value(v)
    return out


def write_config_keys(config_file: Path, updates: dict[str, str]) -> None:
    """Update KEY="VALUE" lines in place, or append missing keys safely (escaped + atomic)."""
    clean_updates: dict[str, str] = {}
    for key, val in updates.items():
        if not CONFIG_KEY_RE.match(str(key)):
            raise ValueError(f"Invalid config key: {key!r}")
        clean_updates[str(key)] = _escape_env_value(str(val))

    lines = config_file.read_text(encoding="utf-8", errors="replace").splitlines() if config_file.is_file() else []
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in clean_updates:
            key = m.group(1)
            out_lines.append(f'{key}="{clean_updates[key]}"')
            seen.add(key)
        else:
            out_lines.append(line)
    for key, escaped_val in clean_updates.items():
        if key not in seen:
            out_lines.append(f'{key}="{escaped_val}"')
    _atomic_write_text(config_file, "\n".join(out_lines) + "\n")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def get_udev_properties(device: str, timeout: int = 5) -> dict[str, str]:
    props: dict[str, str] = {}
    if not shutil.which("udevadm"):
        return props
    try:
        res = subprocess.run(
            ["udevadm", "info", "-q", "property", "-n", device],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
    except Exception:
        pass
    return props


def find_device_holders(device: str) -> list[tuple[int, str]]:
    """Return list of (pid, comm) holding an open FD to `device` (excluding current process)."""
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


def indi_holds_any_serial() -> bool:
    """Return True if indiserver / indi_lx200 holds any /dev/ttyUSB* or /dev/ttyACM* FD."""
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            comm = (Path(entry.path) / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if "indi_lx200" not in comm and "indiserver" not in comm:
            continue
        fd_dir = Path(entry.path) / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = os.path.realpath(str(fd))
                    if target.startswith("/dev/ttyUSB") or target.startswith("/dev/ttyACM"):
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def _cli(argv: list[str]) -> int:
    if not argv:
        return 2
    cmd = argv[0]
    if cmd == "indi-active":
        return 0 if indi_holds_any_serial() else 1
    if cmd == "get-config" and len(argv) in {3, 4}:
        default = argv[3] if len(argv) == 4 else ""
        vals = parse_env_file(Path(argv[1]))
        print(vals.get(argv[2], default))
        return 0
    if cmd == "set-config" and len(argv) == 4:
        write_config_keys(Path(argv[1]), {argv[2]: argv[3]})
        return 0
    if cmd == "write-json" and len(argv) == 3:
        write_json_atomic(Path(argv[1]), json.loads(argv[2]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
