/* OnStepX Extended Configuration for NodeMCU-32S / ESP32 DevKit (PINMAP OFF) */

#ifndef SERIAL_A
  #define SERIAL_A                    Serial
#endif

// Standalone TMC2208S drivers do not use UART; override TmcStepDirDriver.h's
// SoftSerial default so ESP32 does not try to include <SoftwareSerial.h>.
#ifndef SERIAL_TMC
  #define SERIAL_TMC                  OFF
#endif

#ifndef AXIS1_ENABLE_STATE
  #define AXIS1_ENABLE_STATE          LOW
#endif
#ifndef AXIS2_ENABLE_STATE
  #define AXIS2_ENABLE_STATE          LOW
#endif

#ifndef STATUS_LED_ON_STATE
  #define STATUS_LED_ON_STATE         HIGH
#endif

#define DEBUG                         OFF

#define FileVersionExtendedConfig 6
