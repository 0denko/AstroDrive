# AstroDrive

A Raspberry Pi and ESP32 telescope mount controller.

## Layout

- `esp32/firmware`: Arduino firmware for two step/dir stepper drivers and newline-delimited serial commands.
- `backend/api`: FastAPI service that publishes mount commands over MQTT and serves camera configuration.
- `frontend/ui`: Vite + React operator interface.

## Quick start

1. Start an MQTT broker reachable by the Raspberry Pi.
2. Flash the ESP32 firmware using the instructions in `esp32/README.md`.
3. Install and run the API using `backend/README.md`.
4. Install and build the UI using `frontend/README.md`.

For the shortest Raspberry Pi path, follow `deploy/RASPBERRY_PI.md`: flash Raspberry Pi OS Lite 64-bit, SSH in, and run one installer command.

This is a control baseline. Verify motor driver wiring, current limits, travel limits, and an emergency stop before connecting a telescope.
