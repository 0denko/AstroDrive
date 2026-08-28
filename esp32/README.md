# ESP32 Firmware

The firmware targets Arduino-compatible ESP32 and ESP8266 boards driving step/dir motor drivers.

## Flash

The Pi installer uses PlatformIO to compile and upload firmware when the `esp32/` directory changes. Two environments are defined:

```bash
cd esp32/firmware
pio run -e esp32dev     # ESP32-WROOM and friends, the default
pio run -e nodemcuv2    # Amica / LoLin NodeMCU, ESP8266
pio run -e nodemcuv2 -t upload
```

A USB development board with automatic reset/bootloader support can flash unattended. Bare modules may require holding BOOT during the first upload.

For manual development, open `esp32/firmware` in Arduino IDE or PlatformIO, select the ESP32 board and serial port, then upload. The PlatformIO sources are in `src/`.

## Wiring

Pins are set from the web UI (Setup tab) and stored on the board, so `main.cpp` does not need editing. The fields take **GPIO numbers**, not the silkscreen labels: a NodeMCU `D7` is GPIO13 and `D8` is GPIO15.

| | ESP32 default | NodeMCU default |
| --- | --- | --- |
| RA step / dir / enable | 25 / 26 / 27 | 5 / 4 / 16 (D1 / D2 / D0) |
| DEC step / dir / enable | 14 / 12 / 13 | 14 / 12 / 13 (D5 / D6 / D7) |

Both chips wire GPIO 6-11 to the SPI flash and GPIO 1/3 to the USB console, and the firmware and the API both refuse those. Beyond that, some pins are only unsafe at reset, so they are accepted but not chosen as defaults:

- ESP8266 `D8`/GPIO15 must be **low** at boot, `D3`/GPIO0 and `D4`/GPIO2 must be **high**. A driver holding the wrong level stops the board booting.
- ESP32 GPIO12 selects the flash voltage at reset and must not be pulled high.
- GPIO16 on an ESP8266 has a pull-down rather than a pull-up, so an active-low ENABLE sitting there energises the coils until `setup()` runs. Fit an external pull-up to VIO on any ENABLE line.

Confirm your driver logic and enable polarity before powering motors. A TMC2208 `EN` is active **low**.

## Serial protocol

At `115200` baud over the USB cable to the Pi, send newline-delimited commands: `enable`, `disable`, `stop`, `halt`, `zero`, `status`, `move ra forward 10` / `move dec backward 10`, `track ra 1.5`, `speed 2000`, `accel 4000`, or `configure 25 26 27 14 12 13 1`. Configuration is stored in ESP32 NVS or ESP8266 EEPROM and restored after reboot. `status` reports the pins currently in use. Responses are compact JSON lines. Motion planning, encoders, and limits should be added before closed-loop operation.
