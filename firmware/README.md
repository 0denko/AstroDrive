# OnStepX Firmware Build Guide (NodeMCU ESP8266 + TMC2209 Standalone)

This directory builds a reproducible **OnStepX** `.bin` image and SHA-256 `manifest.json` tailored for the **NodeMCU v2/v3 (ESP8266 ESP-12E)** paired with **2× TMC2209 StepStick drivers** in **Standalone Legacy mode (`TMC2209S`)**.

## 1. Configure Your TMC2209 Drivers & `Config.h`

Because the ESP8266 has no spare UART for TMC2209 software control, the TMC2209 runs in **Standalone Step/Dir mode (`TMC2209S`)** — you still get **ultra-silent StealthChop2** and **256× MicroPlyer interpolation** automatically!

### A. Microstepping (`MS1` / `MS2` Hardware Strapping)

| `MS1` Pin | `MS2` Pin | Input Microsteps | Internal Output (MicroPlyer) | Recommendation on ESP8266 |
| :---: | :---: | :---: | :---: | :--- |
| **`GND`** *(or open)* | **`GND`** *(or open)* | **1/8** | **1/256** (interpolated) | **Best for fast slews** (lowest ESP8266 CPU load) |
| **`3V3`** | **`3V3`** | **1/16** | **1/256** (interpolated) | **Balanced default** (matches `Config.h` example) |
| **`3V3`** | **`GND`** | **1/32** | **1/256** (interpolated) | Higher tracking granularity, slower max slew |
| **`GND`** | **`3V3`** | **1/64** | **1/256** (interpolated) | Avoid on ESP8266 unless gearing is very low |

- Leave `PDN_UART`, `CLK`, `DIAG`, and `INDEX` **unconnected** in standalone mode (leaving `SPREAD` unconnected keeps **StealthChop2** enabled).

### B. Set Motor Current via `Vref` Potentiometer (Mandatory!)

With standard `0.11 Ω` (`R110`) sense resistors on TMC2209 StepSticks:

$$V_{\text{ref}} \approx I_{\text{RMS}} \times 1.41 \qquad \left(I_{\text{RMS}} = \frac{V_{\text{ref}}}{2.5} \times 1.77\text{ A}\right)$$

1. **Disconnect stepper motors** before tuning `Vref`.
2. Power **both** `VM` (`12 V`) and `VIO` (`3V3` from NodeMCU) with common `GND`.
3. Measure DC voltage between the **potentiometer metal screw** (or `VREF` via) and `GND`.
4. For AstroDrive's **`36H20HM-0504A` (`0.5 A` rated)** motors, start around **`Vref = 0.35 V`** (`~0.25 A RMS`, cool & `5.4 N·m` at 144:1); do **not** exceed **`0.71 V`** (`0.50 A` rated, see [`../HARDWARE.md`](../HARDWARE.md)).
5. **Power off completely** before connecting/disconnecting motor coils. Always install a **`100 µF` (25–35 V) electrolytic capacitor** across `VM` and `GND` close to each TMC2209.

> [!CAUTION]
> Always power TMC2209 `VIO` from NodeMCU **`3V3`**, never `5V`. ESP8266 GPIOs are **not 5 V tolerant**.

### C. Tune `Config.h` for Your Mount

Edit [`Config.h`](Config.h) (symlink to `profiles/nodemcu-esp8266-tmc2209s/Config.h`) to match your mechanics:

1. **`MOUNT_TYPE`**: `GEM` (German Equatorial), `FORK`, or `ALTAZM`.
2. **`AXIS1_STEPS_PER_DEGREE`** and **`AXIS2_STEPS_PER_DEGREE`**:
   $$\text{Steps/Deg} = \frac{\text{MotorSteps} \times \text{Microsteps} \times \text{GR1} \times \text{GR2}}{360}$$
   *AstroDrive default (`36H20HM` 0.9° = 400 steps/rev, 1/16 microsteps, 144:1 worm, 1:1 belt):* `400 × 16 × 144 ÷ 360 = 2560.0` (or `1280.0` if `MS1`/`MS2` are left open/GND for 1/8 microsteps, or if `36H18HM` is a 1.8° motor).

## 2. Build with `arduino-cli` (Recommended)

`build.sh` pins **ESP8266 Arduino core `2.7.4`** (OnStep's recommended stable core), clones `hjd1964/OnStepX`, injects `Config.h` and `Extended.config.h`, compiles for `nodemcuv2`, and writes the binary + `manifest.json` to `firmware/prebuilt/`:

```bash
chmod +x firmware/build.sh
./firmware/build.sh
```

Copy the resulting artifacts to the Raspberry Pi:

```bash
sudo cp firmware/prebuilt/onstepx-nodemcu-esp8266.bin /opt/onstep-autoprovision/firmware/
sudo cp firmware/prebuilt/manifest.json /opt/onstep-autoprovision/firmware/
```

## 3. Optional: Build with PlatformIO

If you prefer PlatformIO CLI (`pio`):

```bash
git clone --depth 1 https://github.com/hjd1964/OnStepX.git firmware/src/OnStepX
cp firmware/Config.h firmware/Extended.config.h firmware/src/OnStepX/
pio run -d firmware -e nodemcuv2
```

The compiled image is written to `firmware/.pio/build/nodemcuv2/firmware.bin`. Generate `manifest.json` with its `sha256sum` before deploying to `/opt/onstep-autoprovision/firmware/`.

## 4. ESP8266 Runtime Caveats

- **Wi-Fi is disabled (`SERIAL_RADIO OFF`)**: Running OnStepX step interrupts alongside ESP8266 Wi-Fi causes watchdog (WDT) resets and tracking jitter. All mount control runs reliably over USB (`SERIAL_A` @ `9600` baud) to INDI on the Raspberry Pi.
- **Slew Speed**: Keep `SLEW_RATE_BASE_DESIRED` conservative (`1.0`–`1.5` °/s).
- **Future ESP32 Upgrade**: If you later move to an ESP32 board (e.g., **FYSETC E4** or **NodeMCU-32S + MaxESP4**), set `TARGET_CHIP="esp32"` in `/etc/onstep-autoprovision/config.env` and swap in your ESP32 `Config.h` + `.bin` — the Raspberry Pi `udev`/`systemd`/INDI stack remains identical.
