#include <Arduino.h>
#include <cstdlib>
#include <cstring>
#include "MountConfig.h"
#include "StepperMotor.h"

namespace {
constexpr uint32_t SERIAL_BAUD = 115200;

MountConfig config = configDefaults();
StepperMotor raMotor(config.raStep, config.raDir, config.raEnable);
StepperMotor decMotor(config.decStep, config.decDir, config.decEnable);
String commandBuffer;

void loadConfiguration() {
  configLoad(config);
  raMotor.configure(config.raStep, config.raDir, config.raEnable, config.enableActiveLow);
  decMotor.configure(config.decStep, config.decDir, config.decEnable, config.enableActiveLow);
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
                   ",\"pins\":[" + config.raStep + "," + config.raDir + "," + config.raEnable +
                   "," + config.decStep + "," + config.decDir + "," + config.decEnable + "]" +
                   ",\"enable_active_low\":" + (config.enableActiveLow ? "true" : "false") +
                   ",\"configured\":true}");
  } else if (command.startsWith("configure ")) {
    int raStep, raDir, raEnable, decStep, decDir, decEnable, activeLow;
    if (sscanf(command.c_str(), "configure %d %d %d %d %d %d %d", &raStep, &raDir, &raEnable, &decStep, &decDir, &decEnable, &activeLow) == 7 && pinUsable(raStep) && pinUsable(raDir) && pinUsable(raEnable) && pinUsable(decStep) && pinUsable(decDir) && pinUsable(decEnable)) {
      config = MountConfig{static_cast<uint8_t>(raStep), static_cast<uint8_t>(raDir), static_cast<uint8_t>(raEnable),
                           static_cast<uint8_t>(decStep), static_cast<uint8_t>(decDir), static_cast<uint8_t>(decEnable),
                           activeLow != 0};
      configSave(config);
      raMotor.configure(config.raStep, config.raDir, config.raEnable, config.enableActiveLow);
      decMotor.configure(config.decStep, config.decDir, config.decEnable, config.enableActiveLow);
      report("configuration_saved");
    } else {
      fail("invalid_configuration");
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
