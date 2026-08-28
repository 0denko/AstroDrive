#pragma once
#include <Arduino.h>

struct MountConfig {
  uint8_t raStep;
  uint8_t raDir;
  uint8_t raEnable;
  uint8_t decStep;
  uint8_t decDir;
  uint8_t decEnable;
  bool enableActiveLow;
};

// The ESP32 defaults name pins that do not exist on an ESP8266, so they differ per board.
MountConfig configDefaults();
void configLoad(MountConfig &config);
void configSave(const MountConfig &config);

// Rejects pins that are wired to the SPI flash or the USB serial bridge, where a driver
// signal either fails to boot the chip or fights the console.
bool pinUsable(int pin);
