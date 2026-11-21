/* ---------------------------------------------------------------------------------------------------------------------------------
 * AstroDrive — OnStepX Configuration for ESP32-WROOM / NodeMCU-32S + 2x TMC2208S
 *
 * Pinout matches HARDWARE.md directly (zero rewiring required):
 *   GPIO25 : AXIS1_STEP_PIN   (RA STEP)
 *   GPIO26 : AXIS1_DIR_PIN    (RA DIR)
 *   GPIO27 : AXIS1_ENABLE_PIN (RA EN, active LOW; 10 kOhm pull-up to VIO)
 *   GPIO14 : AXIS2_STEP_PIN   (DEC STEP)
 *   GPIO12 : AXIS2_DIR_PIN    (DEC DIR — strapping pin: never add external pull-up!)
 *   GPIO13 : AXIS2_ENABLE_PIN (DEC EN, active LOW; 10 kOhm pull-up to VIO)
 *   GPIO2  : STATUS_LED_PIN   (Onboard ESP32 blue LED, active HIGH)
 *
 * Motors & Mechanics (from HARDWARE.md):
 *   RA : 36H20HM-0504A (0.9 deg = 400 steps/rev, 0.5 A/phase, 12 Ohm)
 *   DEC: 36H18HM       (assumed 0.9 deg = 400 steps/rev; adjust if 1.8 deg)
 *   TMC2208 Standalone (`TMC2208S`): MS1 + MS2 tied to VIO (3V3) -> 1/16 microsteps
 *   (interpolated to 1/256 by MicroPlyer in StealthChop2).
 *   144:1 worm, 1:1 belt -> 2560.0 steps/degree.
 * ---------------------------------------------------------------------------------------------------------------------------------
 */

#define CONFIG_NAME "AstroDrive-ESP32-TMC2208S"

#define PINMAP                        OFF

#define AXIS1_STEP_PIN                25
#define AXIS1_DIR_PIN                 26
#define AXIS1_ENABLE_PIN              27

#define AXIS2_STEP_PIN                14
#define AXIS2_DIR_PIN                 12
#define AXIS2_ENABLE_PIN              13

#define STATUS_LED_PIN                2

#define SERIAL_A_BAUD_DEFAULT         9600
#define SERIAL_B_BAUD_DEFAULT         OFF
#define SERIAL_C_BAUD_DEFAULT         OFF
#define SERIAL_D_BAUD_DEFAULT         OFF
#define SERIAL_E_BAUD_DEFAULT         OFF

#define SERIAL_RADIO                  OFF

#define STATUS_LED                    ON
#define STATUS_MOUNT_LED              ON
#define STATUS_BUZZER                 OFF

#define TIME_LOCATION_SOURCE          OFF
#define TIME_LOCATION_PPS_SENSE       OFF
#define WEATHER                       OFF
#define STEP_WAVE_FORM                PULSE
#define LIMIT_SENSE                   OFF
#define ST4_INTERFACE                 OFF

#define MOUNT_TYPE                    GEM
#define MOUNT_COORDS                  TOPOCENTRIC
#define MOUNT_ENABLE_IN_STANDBY       OFF

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

#define TRACK_AUTOSTART               OFF
#define TRACK_COMPENSATION_DEFAULT    OFF
#define TRACK_BACKLASH_RATE           25
#define SLEW_RATE_BASE_DESIRED        2.5
#define SLEW_RATE_MEMORY              OFF
#define SLEW_ACCELERATION_DIST        5.0
#define SLEW_RAPID_STOP_DIST          2.0
#define GOTO_FEATURE                  ON

#define MFLIP_AUTOMATIC_DEFAULT       OFF
#define MFLIP_PAUSE_HOME_DEFAULT      OFF
#define PIER_SIDE_SYNC_CHANGE_SIDES   OFF
#define PIER_SIDE_PREFERRED_DEFAULT   BEST
#define ALIGN_MAX_STARS               AUTO

#define AXIS3_DRIVER_MODEL            OFF
#define AXIS4_DRIVER_MODEL            OFF
#define AXIS5_DRIVER_MODEL            OFF
#define FEATURE1_PURPOSE              OFF
#define FEATURE2_PURPOSE              OFF
#define FEATURE3_PURPOSE              OFF
#define FEATURE4_PURPOSE              OFF

#define FileVersionConfig 6
