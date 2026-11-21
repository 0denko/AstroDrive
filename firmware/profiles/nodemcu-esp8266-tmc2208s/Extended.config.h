/* ---------------------------------------------------------------------------------------------------------------------------------
 * AstroDrive — OnStepX Extended Configuration for NodeMCU ESP8266 (PINMAP OFF)
 *
 * Explicitly binds SERIAL_A to UART0 (`Serial`) because PINMAP is OFF, and sets
 * active-LOW enable polarity for TMC2208 StepSticks.
 * ---------------------------------------------------------------------------------------------------------------------------------
 */

#ifndef SERIAL_A
  #define SERIAL_A                    Serial
#endif

// Standalone TMC2208S drivers do not use UART; avoid pulling in SoftwareSerial.
#ifndef SERIAL_TMC
  #define SERIAL_TMC                  OFF
#endif

// Active-LOW enable polarity for TMC2208 StepStick carriers
#ifndef AXIS1_ENABLE_STATE
  #define AXIS1_ENABLE_STATE          LOW
#endif
#ifndef AXIS2_ENABLE_STATE
  #define AXIS2_ENABLE_STATE          LOW
#endif

// Keep debug output disabled so UART0 carries clean LX200 protocol traffic only
#define DEBUG                         OFF

#define FileVersionExtendedConfig 6
