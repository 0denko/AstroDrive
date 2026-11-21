#!/usr/bin/env python3
"""
Shared helpers for AstroDrive scripts and Web UI (single source of truth for
/proc/*/fd inspection, udev property lookup, atomic JSON state writes, and
config.env key updates).

Also supports lightweight CLI subcommands so bash scripts (install.sh,
update_software.sh) do not duplicate inline Python heredocs:
  python3 onstep_common.py indi-active
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
from pathlib import Path
from typing import Any


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_config_keys(config_file: Path, updates: dict[str, str]) -> None:
    """Update KEY="VALUE" lines in place, or append missing keys cleanly."""
    config_file.parent.mkdir(parents=True, exist_ok=True)
    lines = config_file.read_text(encoding="utf-8").splitlines() if config_file.is_file() else []
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out_lines.append(f'{key}="{updates[key]}"')
            seen.add(key)
        else:
            out_lines.append(line)
    for key, sval in updates.items():
        if key not in seen:
            out_lines.append(f'{key}="{sval}"')
    tmp = config_file.with_suffix(config_file.suffix + ".tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.replace(tmp, config_file)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
    if cmd == "set-config" and len(argv) == 4:
        write_config_keys(Path(argv[1]), {argv[2]: argv[3]})
        return 0
    if cmd == "write-json" and len(argv) == 3:
        write_json_atomic(Path(argv[1]), json.loads(argv[2]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
