// AstroDrive ESP8266 RF Modem Shutdown Plugin for OnStepX
//
// Disables the ESP8266 Wi-Fi/RF subsystem at startup so:
//   1. Background SDK Wi-Fi calibration interrupts cannot jitter step timing.
//   2. Peak USB current drops from ~170-350 mA to ~50-70 mA (preventing 5V
//      cable-drop USB re-enumeration on the Raspberry Pi).
#pragma once

#include "../../lib/commands/CommandErrors.h"

class Esp8266RfOff {
public:
  void init();
  bool command(char *reply, char *command, char *parameter,
               bool *suppressFrame, bool *numericReply, CommandError *commandError);
};

extern Esp8266RfOff esp8266RfOff;
