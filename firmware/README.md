# OnStepX Firmware Build Guide (ESP32-WROOM & NodeMCU ESP8266 + 2× TMC2208 Standalone)

This directory builds reproducible **OnStepX** `.bin` images and SHA-256 `manifest.json` files tailored for **ESP32-WROOM / NodeMCU-32S** (primary) and **Amica NodeMCU ESP8266** (backup) paired with **2× FYSETC TMC2208 StepStick drivers** in **Standalone Legacy mode (`TMC2208S`)**.

## 1. Configure Your TMC2208 Drivers & `Config.h`

Both profiles run the TMC2208 in **Standalone Step/Dir mode (`TMC2208S`)** — you get **ultra-silent StealthChop2** and **256× MicroPlyer interpolation** automatically without UART wiring.

### A. Microstepping (`MS1` / `MS2` Hardware Strapping on TMC2208)

> [!IMPORTANT]
> TMC2208 and TMC2209 share `1/8` (`LOW/LOW`) and `1/16` (`HIGH/HIGH`), but differ on the other two combinations (`1/2` and `1/4` on TMC2208 vs `1/32` and `1/64` on TMC2209).

| `MS1` Pin | `MS2` Pin | Input Microsteps (TMC2208) | Internal Output (MicroPlyer) | Notes |
| :---: | :---: | :---: | :---: | :--- |
| **`GND`** *(or open)* | **`GND`** *(or open)* | **1/8** | **1/256** (interpolated) | Use `1280.0` steps/deg if unwired |
| **`3V3` (`VIO`)** | **`GND`** | **1/2** | **1/256** (interpolated) | Not recommended for mounts |
| **`GND`** | **`3V3` (`VIO`)** | **1/4** | **1/256** (interpolated) | Not recommended for mounts |
| **`3V3` (`VIO`)** | **`3V3` (`VIO`)** | **1/16** | **1/256** (interpolated) | **AstroDrive default (`2560.0` steps/deg)** |

- Tie **`CLK` → `GND`** (selects the TMC2208 internal clock oscillator).
- Tie **`PDN` → `GND`** (enables automatic standstill current reduction in standalone mode).
- Fit a **`10 kΩ` pull-up resistor from `EN` → `VIO`** on each driver so coils stay disabled while the MCU resets (essential on NodeMCU `D0`/`GPIO16`).

### B. Set Motor Current via `Vref` Potentiometer (Mandatory!)

With standard `0.11 Ω` sense resistors on TMC2208 StepSticks:

$$V_{\text{ref}} \approx I_{\text{RMS}} \times 1.41 \qquad \left(I_{\text{RMS}} = \frac{V_{\text{ref}}}{2.5} \times 1.77\text{ A}\right)$$

1. **Disconnect stepper motors** before tuning `Vref`.
2. Power **both** `VM` (`12 V`, bring up first) and `VIO` (`3V3` from MCU) with common `GND`.
3. Measure DC voltage between the **`Vref` pad / pot screw** and `GND`.
4. For AstroDrive's **`36H20HM-0504A` (`0.5 A` rated)** motors, start around **`Vref = 0.35 V`** (`~0.25 A RMS`, cool & `5.4 N·m` at 144:1); do **not** exceed **`0.71 V`** (`0.50 A` rated, see [`../HARDWARE.md`](../HARDWARE.md)).
5. **Power off completely** before connecting/disconnecting motor coils. Always install a **`100 µF` (25–35 V) electrolytic capacitor** across `VM` and `GND` close to each TMC2208.

> [!CAUTION]
> Always power TMC2208 `VIO` from MCU **`3V3`**, never `5V`. ESP32 and ESP8266 GPIOs are **not 5 V tolerant**.

### C. Tune `Config.h` for Your Mount

Edit [`profiles/nodemcu-esp32-tmc2208s/Config.h`](profiles/nodemcu-esp32-tmc2208s/Config.h) (and [`profiles/nodemcu-esp8266-tmc2208s/Config.h`](profiles/nodemcu-esp8266-tmc2208s/Config.h) if using the NodeMCU backup) to match your mechanics:

1. **`MOUNT_TYPE`**: `GEM` (German Equatorial), `FORK`, or `ALTAZM`.
2. **`AXIS1_STEPS_PER_DEGREE`** and **`AXIS2_STEPS_PER_DEGREE`**:
   $$\text{Steps/Deg} = \frac{\text{MotorSteps} \times \text{Microsteps} \times \text{GR1} \times \text{GR2}}{360}$$
   *AstroDrive default (`36H20HM` 0.9° = 400 steps/rev, 1/16 microsteps, 144:1 worm, 1:1 belt):* `400 × 16 × 144 ÷ 360 = 2560.0` (or `1280.0` if `MS1`/`MS2` are left open/GND for 1/8 microsteps, or if `36H18HM` is a 1.8° motor).

## 2. Build with `arduino-cli` (Recommended)

`build.sh` pins **ESP32 core `2.0.17`** and **ESP8266 core `2.7.4`** (OnStep's recommended stable cores), clones `hjd1964/OnStepX`, overlays the profile `Config.h`, `Extended.config.h`, and optional `plugins/`, compiles, and emits `.bin` + `manifest.json` under `firmware/prebuilt/<profile>/`:

```bash
chmod +x firmware/build.sh
./firmware/build.sh --all
```

Or build a single profile:

```bash
./firmware/build.sh --profile nodemcu-esp32-tmc2208s
./firmware/build.sh --profile nodemcu-esp8266-tmc2208s
```

## 3. Board Notes

- **ESP32-WROOM (Recommended primary):** Dual-core `240 MHz`, hardware timers, onboard status LED on `GPIO2`, `SLEW_RATE_BASE_DESIRED 2.5`, and spare pins for future accessories. Never add an external pull-up to `GPIO12` (DEC DIR strapping pin).
- **NodeMCU ESP8266 (Backup):** Runs at `160 MHz` with `STEP_WAVE_FORM PULSE` and the bundled `esp8266RfOff` plugin (`WiFi.forceSleepBegin()`) to keep Wi-Fi off and prevent WDT/tracking jitter.
