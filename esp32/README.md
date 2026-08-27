# ESP32 Firmware

The firmware targets Arduino-compatible ESP32 boards and step/dir motor drivers.

## Flash

The Pi installer uses PlatformIO to compile and upload firmware when the `esp32/` directory changes. The default board is `esp32dev`; change `board` in `platformio.ini` for a different ESP32 board. A USB development board with automatic reset/bootloader support can flash unattended. Bare modules may require holding BOOT during the first upload.

For manual development, open `esp32/firmware` in Arduino IDE or PlatformIO, select the ESP32 board and serial port, then upload. The PlatformIO sources are in `src/`.

## Wiring

Update the pin constants in `main.cpp` for your board. The default pins are RA step/dir/enable `25/26/27` and DEC step/dir/enable `14/12/13`. Confirm your driver logic and enable polarity before powering motors.

## Serial protocol

At `115200` baud over the USB cable to the Pi, send newline-delimited commands: `enable`, `disable`, `stop`, `status`, `move ra forward 10` / `move dec backward 10`, or `configure 25 26 27 14 12 13 1`. Configuration is stored in ESP32 NVS flash and restored after reboot. Responses are compact JSON lines. Motion planning, encoders, and limits should be added before closed-loop operation.
