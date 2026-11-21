// AstroDrive ESP8266 RF Modem Shutdown Plugin for OnStepX

#include "Esp8266RfOff.h"
#include "../../Common.h"

#ifdef ESP8266
  #include <ESP8266WiFi.h>
#endif

void Esp8266RfOff::init() {
#ifdef ESP8266
  VLF("MSG: Plugins, esp8266RfOff: powering down ESP8266 RF/Wi-Fi modem");
  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
  WiFi.forceSleepBegin();
  delay(1);
#endif
}

bool Esp8266RfOff::command(char *reply, char *command, char *parameter,
                           bool *suppressFrame, bool *numericReply, CommandError *commandError) {
  UNUSED(*reply);
  UNUSED(*command);
  UNUSED(*parameter);
  UNUSED(*suppressFrame);
  UNUSED(*numericReply);
  UNUSED(*commandError);
  return false;
}

Esp8266RfOff esp8266RfOff;
