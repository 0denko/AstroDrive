#include <Arduino.h>
#include <cstdlib>
#include <cstring>
#include <Preferences.h>
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
Preferences preferences;

void loadConfiguration() {
  preferences.begin("astrodrive", true);
  raMotor.configure(preferences.getUChar("ras", RA_STEP), preferences.getUChar("rad", RA_DIR), preferences.getUChar("rae", RA_ENABLE), preferences.getBool("enlow", true));
  decMotor.configure(preferences.getUChar("decs", DEC_STEP), preferences.getUChar("decd", DEC_DIR), preferences.getUChar("dece", DEC_ENABLE), preferences.getBool("enlow", true));
  preferences.end();
}

void report(const String &message) {
  Serial.println(String("{\"ok\":true,\"message\":\"") + message + "\"}");
}

void fail(const char *error) {
  Serial.println(String("{\"ok\":false,\"error\":\"") + error + "\"}");
}

StepperMotor *resolveAxis(const char *axis) {
  if (strcmp(axis, "ra") == 0) return &raMotor;
  if (strcmp(axis, "dec") == 0) return &decMotor;
  return nullptr;
}

String axisStatus(const StepperMotor &motor) {
  return String("{\"moving\":") + (motor.isMoving() ? "true" : "false") +
         ",\"position\":" + motor.position() +
         ",\"speed\":" + String(motor.speed(), 2) + "}";
}

void handleCommand(String command) {
  command.trim();
  if (command == "enable") {
    raMotor.setEnabled(true);
    decMotor.setEnabled(true);
    report("motors_enabled");
  } else if (command == "stop") {
    // Ramp down but stay energised; cutting current drops an unbalanced load.
    raMotor.stop();
    decMotor.stop();
    report("motion_stopping");
  } else if (command == "halt") {
    raMotor.halt();
    decMotor.halt();
    report("motion_halted");
  } else if (command == "disable") {
    raMotor.setEnabled(false);
    decMotor.setEnabled(false);
    report("motors_disabled");
  } else if (command == "zero") {
    raMotor.setPosition(0);
    decMotor.setPosition(0);
    report("position_zeroed");
  } else if (command == "status") {
    Serial.println(String("{\"ok\":true,\"enabled\":") +
                   (raMotor.isEnabled() && decMotor.isEnabled() ? "true" : "false") +
                   ",\"ra\":" + axisStatus(raMotor) +
                   ",\"dec\":" + axisStatus(decMotor) +
                   ",\"configured\":true}");
  } else if (command.startsWith("configure ")) {
    int raStep, raDir, raEnable, decStep, decDir, decEnable, activeLow;
    if (sscanf(command.c_str(), "configure %d %d %d %d %d %d %d", &raStep, &raDir, &raEnable, &decStep, &decDir, &decEnable, &activeLow) == 7 && raStep >= 0 && raDir >= 0 && raEnable >= 0 && decStep >= 0 && decDir >= 0 && decEnable >= 0) {
      preferences.begin("astrodrive", false);
      preferences.putUChar("ras", raStep); preferences.putUChar("rad", raDir); preferences.putUChar("rae", raEnable);
      preferences.putUChar("decs", decStep); preferences.putUChar("decd", decDir); preferences.putUChar("dece", decEnable); preferences.putBool("enlow", activeLow != 0);
      preferences.end();
      raMotor.configure(raStep, raDir, raEnable, activeLow != 0);
      decMotor.configure(decStep, decDir, decEnable, activeLow != 0);
      report("configuration_saved");
    } else {
      Serial.println("{\"ok\":false,\"error\":\"invalid_configuration\"}");
    }
  } else if (command.startsWith("move ")) {
    char axis[8] = {};
    char direction[10] = {};
    int steps = 0;
    if (sscanf(command.c_str(), "move %7s %9s %d", axis, direction, &steps) == 3 && steps >= 0) {
      StepperMotor *motor = resolveAxis(axis);
      if (motor == nullptr) {
        fail("invalid_axis");
        return;
      }
      const bool forward = strcmp(direction, "forward") == 0;
      if (!forward && strcmp(direction, "backward") != 0) {
        fail("invalid_direction");
        return;
      }
      motor->moveBy(forward ? steps : -steps);
      report("move_started");
    } else {
      fail("invalid_move");
    }
  } else if (command.startsWith("track ")) {
    char axis[8] = {};
    char rate[16] = {};
    if (sscanf(command.c_str(), "track %7s %15s", axis, rate) == 2) {
      StepperMotor *motor = resolveAxis(axis);
      if (motor == nullptr) {
        fail("invalid_axis");
        return;
      }
      motor->setContinuousRate(atof(rate));
      report("tracking_set");
    } else {
      fail("invalid_track");
    }
  } else if (command.startsWith("speed ")) {
    char value[16] = {};
    if (sscanf(command.c_str(), "speed %15s", value) == 1 && atof(value) > 0.0) {
      raMotor.setMaxSpeed(atof(value));
      decMotor.setMaxSpeed(atof(value));
      report("speed_set");
    } else {
      fail("invalid_speed");
    }
  } else if (command.startsWith("accel ")) {
    char value[16] = {};
    if (sscanf(command.c_str(), "accel %15s", value) == 1 && atof(value) > 0.0) {
      raMotor.setAcceleration(atof(value));
      decMotor.setAcceleration(atof(value));
      report("acceleration_set");
    } else {
      fail("invalid_acceleration");
    }
  } else {
    fail("unknown_command");
  }
}
}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  loadConfiguration();
  raMotor.begin();
  decMotor.begin();
  report("ready");
}

void loop() {
  raMotor.run();
  decMotor.run();

  while (Serial.available() > 0) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n') {
      handleCommand(commandBuffer);
      commandBuffer = "";
      break;  // one command per pass so the motors keep stepping
    }
    if (character != '\r' && commandBuffer.length() < 128) {
      commandBuffer += character;
    }
  }
}
