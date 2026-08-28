#include "MountConfig.h"

#if defined(ARDUINO_ARCH_ESP8266)
#include <EEPROM.h>
namespace {
constexpr uint16_t kStoreSize = 16;
constexpr uint8_t kMagic = 0xA7;
}  // namespace
#else
#include <Preferences.h>
namespace {
Preferences preferences;
}  // namespace
#endif

MountConfig configDefaults() {
#if defined(ARDUINO_ARCH_ESP8266)
  // D1 D2 D0 / D5 D6 D7. Once the flash pins, the USB serial pair and the pins that must hold a
  // level at reset are excluded, these are the only NodeMCU pins left that are free at boot.
  return MountConfig{5, 4, 16, 14, 12, 13, true};
#else
  return MountConfig{25, 26, 27, 14, 12, 13, true};
#endif
}

void configLoad(MountConfig &config) {
  config = configDefaults();
#if defined(ARDUINO_ARCH_ESP8266)
  EEPROM.begin(kStoreSize);
  if (EEPROM.read(0) == kMagic) {
    config.raStep = EEPROM.read(1);
    config.raDir = EEPROM.read(2);
    config.raEnable = EEPROM.read(3);
    config.decStep = EEPROM.read(4);
    config.decDir = EEPROM.read(5);
    config.decEnable = EEPROM.read(6);
    config.enableActiveLow = EEPROM.read(7) != 0;
  }
  EEPROM.end();
#else
  preferences.begin("astrodrive", true);
  config.raStep = preferences.getUChar("ras", config.raStep);
  config.raDir = preferences.getUChar("rad", config.raDir);
  config.raEnable = preferences.getUChar("rae", config.raEnable);
  config.decStep = preferences.getUChar("decs", config.decStep);
  config.decDir = preferences.getUChar("decd", config.decDir);
  config.decEnable = preferences.getUChar("dece", config.decEnable);
  config.enableActiveLow = preferences.getBool("enlow", config.enableActiveLow);
  preferences.end();
#endif
}

void configSave(const MountConfig &config) {
#if defined(ARDUINO_ARCH_ESP8266)
  EEPROM.begin(kStoreSize);
  EEPROM.write(0, kMagic);
  EEPROM.write(1, config.raStep);
  EEPROM.write(2, config.raDir);
  EEPROM.write(3, config.raEnable);
  EEPROM.write(4, config.decStep);
  EEPROM.write(5, config.decDir);
  EEPROM.write(6, config.decEnable);
  EEPROM.write(7, config.enableActiveLow ? 1 : 0);
  EEPROM.commit();
  EEPROM.end();
#else
  preferences.begin("astrodrive", false);
  preferences.putUChar("ras", config.raStep);
  preferences.putUChar("rad", config.raDir);
  preferences.putUChar("rae", config.raEnable);
  preferences.putUChar("decs", config.decStep);
  preferences.putUChar("decd", config.decDir);
  preferences.putUChar("dece", config.decEnable);
  preferences.putBool("enlow", config.enableActiveLow);
  preferences.end();
#endif
}

bool pinUsable(int pin) {
  if (pin < 0) return false;
  if (pin >= 6 && pin <= 11) return false;  // SPI flash
  if (pin == 1 || pin == 3) return false;   // USB serial
#if defined(ARDUINO_ARCH_ESP8266)
  return pin <= 16;
#else
  return pin <= 33;  // 34-39 exist but are input only
#endif
}
