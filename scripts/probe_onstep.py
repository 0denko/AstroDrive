#!/usr/bin/env python3
"""
Standalone LX200 diagnostic probe for OnStep / OnStepX controllers.

Safely opens the serial port with DTR/RTS disabled (preventing NodeMCU auto-reset)
and queries :GVP# (product name), :GVN# (firmware version), and :GVD# (build date).
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import serial


def send_lx200(ser: serial.Serial, cmd: str, terminator: bytes = b"#") -> str:
    ser.reset_input_buffer()
    ser.write(cmd.encode("ascii"))
    ser.flush()
    raw = ser.read_until(terminator)
    return raw.decode("ascii", errors="replace").strip()


def probe_port(device: str, baud: int, timeout: float = 1.5) -> Optional[dict[str, str]]:
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = timeout
    ser.write_timeout = timeout
    # Prevent NodeMCU CP2102/CH340 DTR+RTS auto-reset circuit from rebooting the MCU
    ser.dtr = False
    ser.rts = False

    try:
        ser.open()
        # Re-assert after open on Linux drivers that toggle modem lines during open()
        ser.dtr = False
        ser.rts = False
        time.sleep(0.15)

        product = send_lx200(ser, ":GVP#")
        if not product.startswith("On-Step"):
            return None

        version = send_lx200(ser, ":GVN#").rstrip("#")
        build_date = send_lx200(ser, ":GVD#").rstrip("#")
        return {
            "baud": str(baud),
            "product": product.rstrip("#"),
            "version": version,
            "date": build_date,
        }
    finally:
        if ser.is_open:
            ser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a serial port for OnStep/OnStepX.")
    parser.add_argument("--device", default="/dev/onstep", help="Serial device path (default: /dev/onstep)")
    parser.add_argument(
        "--bauds",
        default="9600,115200,57600,19200",
        help="Comma-separated baud rates to try (default: 9600,115200,57600,19200)",
    )
    parser.add_argument("--timeout", type=float, default=1.5, help="Read timeout in seconds")
    args = parser.parse_args()

    bauds = [int(b.strip()) for b in args.bauds.split(",") if b.strip()]
    for baud in bauds:
        try:
            info = probe_port(args.device, baud, timeout=args.timeout)
        except serial.SerialException as exc:
            print(f"[ERROR] Could not open {args.device} at {baud} baud: {exc}", file=sys.stderr)
            return 2

        if info:
            print(f"Device:   {args.device}")
            print(f"Baud:     {info['baud']}")
            print(f"Product:  {info['product']}")
            print(f"Version:  {info['version']}")
            print(f"Date:     {info['date']}")
            return 0

    print(f"[FAIL] No OnStep/OnStepX response on {args.device} across bauds {bauds}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
