#include <Arduino.h>
#include "StepperMotor.h"

namespace {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint8_t RA_STEP = 25;
constexpr uint8_t RA_DIR = 26;
constexpr uint8_t RA_ENABLE = 27;
constexpr uint8_t DEC_STEP = 14;
constexpr uint8_t DEC_DIR = 12;
constexpr uint8_t DEC_ENABLE = 13;

StepperMotor raMotor(RA_STEP, RA_DIR, RA_ENABLE);
StepperMotor decMotor(DEC_STEP, DEC_DIR, DEC_ENABLE);
String commandBuffer;

void report(const String &message) {
  Serial.println(String("{\"ok\":true,\"message\":\"") + message + "\"}");
}

void handleCommand(String command) {
  command.trim();
  if (command == "enable") {
    raMotor.setEnabled(true);
    decMotor.setEnabled(true);
    report("motors_enabled");
  } else if (command == "disable" || command == "stop") {
    raMotor.setEnabled(false);
    decMotor.setEnabled(false);
    report("motors_disabled");
  } else if (command == "status") {
    Serial.println(String("{\"ok\":true,\"enabled\":") +
                   (raMotor.isEnabled() && decMotor.isEnabled() ? "true}" : "false}"));
  } else {
    Serial.println("{\"ok\":false,\"error\":\"unknown_command\"}");
  }
}
}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  raMotor.begin();
  decMotor.begin();
  report("ready");
}

void loop() {
  while (Serial.available() > 0) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n') {
      handleCommand(commandBuffer);
      commandBuffer = "";
    } else if (character != '\r' && commandBuffer.length() < 128) {
      commandBuffer += character;
    }
  }
}
