/* ---------------------------------------------------------------------------------------------------------------------------------
 * AstroDrive — OnStepX Configuration for NodeMCU ESP8266 (Amica v2 / LoLin v3) + 2x TMC2208S
 *
 * Pinout matches HARDWARE.md directly (zero rewiring required):
 *   D1 -> GPIO5  : AXIS1_STEP_PIN   (RA STEP)
 *   D2 -> GPIO4  : AXIS1_DIR_PIN    (RA DIR)
 *   D0 -> GPIO16 : AXIS1_ENABLE_PIN (RA EN, active LOW; fit 10 kOhm pull-up to VIO!)
 *   D5 -> GPIO14 : AXIS2_STEP_PIN   (DEC STEP)
 *   D6 -> GPIO12 : AXIS2_DIR_PIN    (DEC DIR)
 *   D7 -> GPIO13 : AXIS2_ENABLE_PIN (DEC EN, active LOW; fit 10 kOhm pull-up to VIO!)
 *
 * Motors & Mechanics (from HARDWARE.md):
 *   RA : 36H20HM-0504A (0.9 deg = 400 steps/rev, 0.5 A/phase, 12 Ohm)
 *   DEC: 36H18HM       (assumed 0.9 deg = 400 steps/rev; adjust if 1.8 deg)
 *   TMC2208 Standalone (`TMC2208S`): MS1 + MS2 tied to VIO (3V3) -> 1/16 microsteps
 *   (interpolated to 1/256 by MicroPlyer in StealthChop2).
 *   144:1 worm, 1:1 belt: 400 * 16 * 144 / 360 = 2560.0 steps/degree.
 *   Set Vref to ~0.35 V (~0.25 A RMS) per driver; never exceed 0.71 V (0.5 A rated).
 * ---------------------------------------------------------------------------------------------------------------------------------
 */

#define CONFIG_NAME "AstroDrive-NodeMCU-ESP8266-TMC2208S"

// PINMAP OFF: custom pin assignments below match HARDWARE.md
#define PINMAP                        OFF

#define AXIS1_STEP_PIN                5    // D1 (GPIO5)  -> RA TMC2208 STEP
#define AXIS1_DIR_PIN                 4    // D2 (GPIO4)  -> RA TMC2208 DIR
#define AXIS1_ENABLE_PIN              16   // D0 (GPIO16) -> RA TMC2208 EN (active LOW; 10k pull-up to 3V3)

#define AXIS2_STEP_PIN                14   // D5 (GPIO14) -> DEC TMC2208 STEP
#define AXIS2_DIR_PIN                 12   // D6 (GPIO12) -> DEC TMC2208 DIR
#define AXIS2_ENABLE_PIN              13   // D7 (GPIO13) -> DEC TMC2208 EN (active LOW)

// SERIAL PORT COMMAND CHANNELS (UART0 USB to Raspberry Pi + indi_lx200_OnStep)
#define SERIAL_A_BAUD_DEFAULT         9600
#define SERIAL_B_BAUD_DEFAULT         OFF
#define SERIAL_C_BAUD_DEFAULT         OFF
#define SERIAL_D_BAUD_DEFAULT         OFF
#define SERIAL_E_BAUD_DEFAULT         OFF

// Keep radio OFF on ESP8266 so Raspberry Pi / Astroberry OS acts as the Wi-Fi hub
#define SERIAL_RADIO                  OFF

// D0 (GPIO16) is used for RA EN; disable onboard status LED so it doesn't toggle RA EN
#define STATUS_LED                    OFF
#define STATUS_MOUNT_LED              OFF
#define STATUS_BUZZER                 OFF

// Time & location supplied automatically by KStars/Ekos + indi_lx200_OnStep
#define TIME_LOCATION_SOURCE          OFF
#define TIME_LOCATION_PPS_SENSE       OFF
#define WEATHER                       OFF
#define STEP_WAVE_FORM                PULSE   // 1 ISR per step (2x efficiency on ESP8266; TMC2208 needs >=100ns)
#define LIMIT_SENSE                   OFF
#define ST4_INTERFACE                 OFF

// MOUNT TYPE (Equatorial German Mount)
#define MOUNT_TYPE                    GEM
#define MOUNT_COORDS                  TOPOCENTRIC
#define MOUNT_ENABLE_IN_STANDBY       OFF

// AXIS1 RA: 36H20HM (0.9 deg, 400 steps/rev) * 16 microsteps * 144:1 worm / 360 = 2560.0
// (If MS1/MS2 are left open for 1/8 microsteps, use 1280.0 instead.)
#define AXIS1_STEPS_PER_DEGREE        2560.0
#define AXIS1_REVERSE                 OFF
#define AXIS1_LIMIT_MIN               -180
#define AXIS1_LIMIT_MAX               180
#define AXIS1_DRIVER_MODEL            TMC2208S
#define AXIS1_DRIVER_MICROSTEPS       OFF
#define AXIS1_DRIVER_MICROSTEPS_GOTO  OFF
#define AXIS1_DRIVER_IHOLD            OFF
#define AXIS1_DRIVER_IRUN             OFF
#define AXIS1_DRIVER_IGOTO            OFF
#define AXIS1_DRIVER_STATUS           OFF
#define AXIS1_POWER_DOWN              OFF
#define AXIS1_SENSE_HOME              OFF

// AXIS2 DEC: 36H18HM (assumed 0.9 deg, 400 steps/rev) * 16 microsteps * 144:1 / 360 = 2560.0
// (If 36H18HM turns out to be 1.8 deg / 200 steps/rev, set to 1280.0.)
#define AXIS2_STEPS_PER_DEGREE        2560.0
#define AXIS2_REVERSE                 OFF
#define AXIS2_LIMIT_MIN               -90
#define AXIS2_LIMIT_MAX               90
#define AXIS2_DRIVER_MODEL            TMC2208S
#define AXIS2_DRIVER_MICROSTEPS       OFF
#define AXIS2_DRIVER_MICROSTEPS_GOTO  OFF
#define AXIS2_DRIVER_IHOLD            OFF
#define AXIS2_DRIVER_IRUN             OFF
#define AXIS2_DRIVER_IGOTO            OFF
#define AXIS2_DRIVER_STATUS           OFF
#define AXIS2_POWER_DOWN              OFF
#define AXIS2_SENSE_HOME              OFF

// TRACKING & SLEWING (PULSE mode supports 2.0 deg/s cleanly on 160 MHz ESP8266)
#define TRACK_AUTOSTART               OFF
#define TRACK_COMPENSATION_DEFAULT    OFF
#define TRACK_BACKLASH_RATE           25
#define SLEW_RATE_BASE_DESIRED        2.0
#define SLEW_RATE_MEMORY              OFF
#define SLEW_ACCELERATION_DIST        5.0
#define SLEW_RAPID_STOP_DIST          2.0
#define GOTO_FEATURE                  ON

// MERIDIAN FLIP & ALIGNMENT
#define MFLIP_AUTOMATIC_DEFAULT       OFF
#define MFLIP_PAUSE_HOME_DEFAULT      OFF
#define PIER_SIDE_SYNC_CHANGE_SIDES   OFF
#define PIER_SIDE_PREFERRED_DEFAULT   BEST
#define ALIGN_MAX_STARS               AUTO

// ROTATOR / FOCUSERS / AUXILIARY (Disabled on ESP8266)
#define AXIS3_DRIVER_MODEL            OFF
#define AXIS4_DRIVER_MODEL            OFF
#define AXIS5_DRIVER_MODEL            OFF
#define FEATURE1_PURPOSE              OFF
#define FEATURE2_PURPOSE              OFF
#define FEATURE3_PURPOSE              OFF
#define FEATURE4_PURPOSE              OFF

#define FileVersionConfig 6
#include "Extended.config.h"
