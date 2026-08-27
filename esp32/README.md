# ESP32 Firmware

The firmware targets Arduino-compatible ESP32 boards and step/dir motor drivers.

## Flash

Open `esp32/firmware` in Arduino IDE or PlatformIO, select the ESP32 board and serial port, then upload. Add `libraries/StepperMotor.cpp` and `libraries/StepperMotor.h` to the sketch if using Arduino IDE.

## Wiring

Update the pin constants in `main.cpp` for your board. The default pins are RA step/dir/enable `25/26/27` and DEC step/dir/enable `14/12/13`. Confirm your driver logic and enable polarity before powering motors.

## Serial protocol

At `115200` baud, send newline-delimited commands: `enable`, `disable`, `stop`, or `status`. Responses are compact JSON lines. Motion planning and limits should be added before closed-loop operation.
