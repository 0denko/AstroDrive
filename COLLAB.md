# COLLAB.md — Shared Notebook

This file is a shared notebook between two AI assistants working on AstroDrive:

- **Bingo** — the user's Muse assistant. Works through GitHub (reads/writes files, reviews code, leaves notes here).
- **Antigravity** — Google's agentic IDE. Works locally in the user's workspace.

## Ground rules

1. Append new entries at the bottom. Do not rewrite or delete the other assistant's entries.
2. Start each entry with your name and the date, e.g. `## Bingo — 2026-11-20`.
3. Be specific: name files, quote the behavior, say what should change and why.
4. If you act on the other assistant's note, add a short reply entry saying what you did.
5. Neither assistant monitors this file continuously. The user relays between us — expect delays.

---

## Bingo — 2026-11-20 — Review of setup.sh

I reviewed `setup.sh` (the one-line installer added in commit 4cce634). Overall it is solid: `set -euo pipefail`, re-runs itself with sudo when needed, safe to run twice (updates in place via fetch + reset), and delegates the real work to `install.sh`. Two findings:

1. **Silent firmware failure.** Step 4 runs `/opt/onstep-autoprovision/scripts/update_firmware.sh --all || true`, so if the OnStepX firmware download fails, the script still prints \"AstroDrive is installed and ready!\" The Setup Wizard's System Check may catch the missing firmware, but the success message alone can mislead a beginner. Suggestion: track the failure and print a clear warning in the final banner (e.g. \"firmware download FAILED — connect the Pi to the internet and re-run setup.sh\").

2. **Updates wipe local changes without warning.** On re-run, `git reset --hard origin/main` in `/opt/astrodrive-src` discards any local edits silently. Acceptable for an appliance-style install, but a one-line echo before the reset would set expectations.

Note: `setup.sh` is mostly a wrapper. A full review of the deployment path (`install.sh`, the firmware update script, the wizard backend, systemd/udev setup) is in progress; I will append those findings here when done.

— Bingo

---

## Bingo — 2026-11-20 — Full deployment review (install.sh, firmware pipeline, wizard, systemd/udev)

I read the full deployment path: `install.sh`, `scripts/update_firmware.sh`, `scripts/onstep_provision.py`, `scripts/probe_onstep.py`, `scripts/configure_indi_defaults.sh`, `scripts/configure_nginx_overlay.sh`, `scripts/setup_hotspot.sh`, `webui/onstep_webui.py`, `webui/index.html`, the systemd units, the udev rules, `firmware/build.sh`, and the GitHub workflows. The core engineering is genuinely careful (see "What looks good" at the end), but there are three must-fix issues:

### Must-fix

**M1. The Web Setup Wizard is an unauthenticated root service on all interfaces, and it can repoint the firmware source and trigger a flash.** `systemd/onstep-webui.service` has no `User=` (runs as root), no sandboxing, and `WEBUI_HOST=0.0.0.0`, port 8090. `webui/onstep_webui.py` has no auth on any route. Anyone who can reach the Pi (home LAN or the field hotspot) can: `POST /api/config` (rewrite `GITHUB_REPO` — the regex allows ANY owner/repo — and `AUTO_FLASH`), `POST /api/action/update-cache`, `POST /api/action/provision` (flash the board), `POST /api/action/sync-time` (set system time as root), `POST /api/action/setup-hotspot`. These chain: set `GITHUB_REPO` to an attacker repo → update-cache → provision = flash attacker firmware, and the SHA-256 check does not stop this (see S1). The nginx overlay proxies this same service, so `astroberry.local` adds no protection. Note the contrast: `onstep-autoprovision@.service` IS hardened (NoNewPrivileges, ProtectSystem=strict, PrivateTmp) — the web UI is the weak link.

**M2. A fresh one-line install can ship zero local firmware while declaring success.** The repo contains no prebuilt bundles (`firmware/prebuilt/` holds only `manifest.json.example`), so `install.sh`'s prebuilt-copy loop installs nothing. Tier 3 (on-Pi compile) is unavailable by default because `setup.sh` doesn't pass `--with-arduino-cli`, yet `config.env` defaults `AUTO_COMPILE="true"`, so `onstep_provision.py` just logs "arduino-cli is not installed" and gives up. Everything depends on the Tier-1 release fetch in setup.sh step 4, which runs with `|| true`, and `update_firmware.sh` exits 0 with only an `[INFO]` line when offline or when no release exists. setup.sh then prints "AstroDrive is installed and ready!", contradicting README Step 3. The wizard's Screen 1 warning ("Firmware not cached yet") is not a gate — the user can click through to Screen 3 and wait forever for a flash that can't happen. (Only the CI-built .img.xz path avoids this.)

**M3. The default install arms auto-flash for ANY CP2102/CH340 device, with the safety allowlist empty.** `setup.sh` always passes `--enable-auto-flash` → `AUTO_FLASH="true"`. The udev rules trigger provisioning for every tty with VID:PID `10c4:ea60` (CP2102) or `1a86:7523` (CH340) — the two most common USB-UART chips in hobby electronics. `config.env` ships `ALLOWED_USB_SERIALS=""` and `ALLOWED_USB_PATHS=""`, and `check_usb_allowlist` returns True when both are empty. Any non-OnStep ESP board on one of those chips that doesn't answer `:GVP#` gets flashed with OnStepX. This contradicts the config template's own guidance ("keep false until you have verified your USB filter/wiring", "STRONGLY RECOMMENDED" allowlist). The one-line path silently overrides both recommendations.

### Should-fix

**S1. "Verified" firmware is checked only against a self-asserted manifest, and the pipeline is unpinned.** `update_firmware.sh` verifies the .bin against a manifest downloaded from the same release, unsigned — whoever controls the release controls both. `build-firmware.yml` builds from `hjd1964/OnStepX` branch `main` (moving ref) and republishes a `--latest` release on every firmware-touching push and weekly; every Pi auto-downloads within 24h (timer: OnBootSec=3min, OnUnitActiveSec=24h). The manifest records `onstepx_commit` but nothing checks it against an expected value. The SHA-256 check proves the .bin matches the manifest it arrived with, nothing more — "verified" in setup.sh/README overstates it.

**S2. Overlay and INDI-defaults failures are swallowed at every layer.** `install.sh` runs `configure_nginx_overlay.sh || true` and `configure_indi_defaults.sh || true`. The overlay script itself exits 0 after a rollback and when no server block is found. If the overlay attach fails, there is no "AstroDrive Setup" button and no error a beginner will see — README Step 4's primary instruction dead-ends; only the :8090 fallback (mentioned in troubleshooting, not Step 4) works. The APT post-invoke hook re-runs the overlay script with `|| true` after every apt operation, failures invisible.

**S3. udev coverage doesn't match the README's "ESP32-WROOM (recommended)" promise.** Rules match only CP2102 and CH340. Boards with CH9102 (`1a86:55d4`) or native USB-CDC (`303a:1001`, common on ESP32-S2/S3/C3 and many current WROOM devkits) never trigger provisioning, and the wizard sits at "Waiting for your ESP32 or NodeMCU…" with no hint the board was seen-but-unmatched. `onstep_provision.py` also has no profile mapping for esp32s3/esp32c3 (refuses safely, but silently). Separately, `SYMLINK+="onstep-raw"` is created for ANY matching-chip device, so with two such devices attached the symlink (and the web UI's `resolve_active_tty`, which prefers it) can point at the wrong device.

**S4. `AUTO_UPGRADE` cannot work as documented.** With `AUTO_UPGRADE=true` but `AUTO_FLASH=false` (the config's own default pairing), a working board is never upgraded — in `onstep_provision.py:main` the working-board path publishes the symlink and returns when `auto_flash` is false. Upgrade silently requires both flags. The version-skip comparison is also fragile: `build.sh` synthesizes `firmware_version` from greps with hard fallbacks (e.g. `10.25a`), so a parse failure produces a plausible-but-wrong version and the equality check misfires either way.

**S5. `update_firmware.sh` hides failures and leaks state.** In `--all` mode, per-profile failures are discarded (`|| true` per profile) and the script exits 0, so the systemd service reports success even when a profile failed its SHA-256 check. In single-profile mode, a failure under `set -e` exits before the final `rm -f /tmp/onstep_release.json`, leaking the temp file; that path is also a predictable /tmp file written by root, and `onstep-firmware-update.service` has no `PrivateTmp=yes`.

### Minor

- m1. `install.sh`'s sed edits for `AUTO_FLASH`/`GITHUB_REPO` silently no-op if a preserved `config.env` lacks the key (the web UI's updater appends missing keys correctly; the installer doesn't).
- m2. Stale venv on re-run: `.venv` is created only if missing, then pip-installed into unconditionally — after a Python minor-version upgrade the services can run against a broken venv while install reports success.
- m3. `build.sh` ESP8266 binary selection is arbitrary (`find ... | head -n 1`) instead of the known sketch binary name used in the ESP32 branch.
- m4. Known default hotspot passwords: `setup_hotspot.sh` defaults to `astropi1234` when run bare, and `build-pi-image.yml` ships a default `HOTSPOT_PASS` of `clearSkies2026` printed in the repo. (The wizard path enforces user-chosen ≥8-char passwords; this only bites CLI/image users.)
- m5. `configure_nginx_overlay.sh` inserts the include into EVERY `server {}` block in the target file, not just Astroberry's.
- m6. README: "Wait 1–2 minutes" for setup.sh is optimistic (apt update + packages + pip install of esptool/pyserial on a Pi). The dark-sky hotspot tip presumes the user completed the OPTIONAL wizard Screen 2; nothing says the hotspot doesn't exist otherwise.
- m7. Optional compiler path runs unverified root code: `install.sh --with-arduino-cli` pipes the arduino-cli install script from GitHub into `sh` as root. Not in the default path, but README documents the flag.

### What looks good

- `update_firmware.sh` download discipline: mktemp dir, SHA-256 verified BEFORE installing into cache, existing cache untouched on failure, short-circuits when cached SHA matches, clean offline behavior.
- `onstep_provision.py` safety design: exclusive flock; refuses ports held by INDI/gpsd/ModemManager; probes with DTR/RTS held false (never resets the MCU); verifies `:GVP#` AFTER flashing before publishing `/dev/onstep`; atomic symlink publish; refuses unidentifiable chips; esptool baud fallback 460800 → 115200; sensible timeouts.
- `onstep-autoprovision@.service` hardening and the udev design (SYSTEMD_WANTS instead of RUN+=, ID_MM_DEVICE_IGNORE=1, symlink cleanup on unplug).
- `configure_nginx_overlay.sh` mechanics (backup → edit → nginx -t → rollback) — correct pattern; the flaw is only that failure is then hidden (S2).
- `configure_indi_defaults.sh`: create-only by default, correct /dev/onstep @ 9600 content, proper ownership, covers all /home users.
- Web UI input handling (within its unauthenticated design): strict allowlist + regex validation, 32 KB body cap, hotspot password checks, HTTP 409 while INDI holds the mount.
- Cross-file consistency: CI release asset names match `update_firmware.sh` expectations; ESP32 merged-image offsets match profile.env/manifest; `install.sh` installs every referenced file (none missing); unit names line up across install.sh, web UI, and README.

### Verdict

The provisioning core is unusually careful for a hobby project. But the deployment layer undermines the "Next → Next → Done" promise in three ways: the wizard is an unauthenticated root service whose config endpoint can repoint the firmware source that the SHA-256 check then "verifies" against itself; the headline one-line install can finish with no firmware on the Pi while printing "installed and ready"; and that same install silently arms auto-flash for any CP2102/CH340 device with the documented safety allowlist left empty. Fix M1–M3 and surface the S2 silent failures in the wizard, and the system matches its README.

One thing I could NOT verify: whether a GitHub Release actually exists for this repo (the connector's read tools have no release-listing operation). M2 is assessed from how the scripts handle both cases.

— Bingo

---

## Bingo — 2026-11-20 — Suggestion: self-update for the AstroDrive software itself

Question from the user: if AstroDrive changes in this repo, how does an already-provisioned Pi find out and update?

Current state (verified against the repo): only the OnStepX **firmware** self-updates. `systemd/onstep-firmware-update.timer` fires 3 min after boot and every 24h, and `update_firmware.sh` pulls the latest GitHub Release into the local cache. The AstroDrive **software** (scripts, wizard, web UI, units) has no update check at all — the only path is a human re-running the one-line `setup.sh` on each Pi.

Suggestion: add a lightweight software self-update, mirroring the firmware pattern:

1. A `astrodrive-update.timer` / `.service` pair (same cadence: a few minutes after boot, then daily, `Persistent=true`, skips cleanly when offline).
2. The service compares the installed version (e.g. a `VERSION` file or the git SHA recorded by `install.sh` in `/opt/astrodrive-src`) against the latest release/tag on GitHub. If unchanged, exit quietly.
3. If changed, re-run the existing path: fetch + reset `/opt/astrodrive-src`, then `install.sh` with the same flags the original install used (persist those flags at install time so the update reproduces them, especially `--enable-auto-flash` and the github repo).
4. Safety: never restart the web UI or touch a board mid-session — check that no provision/flash is in progress (the provision flock can signal this) and that INDI is not connected; defer to the next cycle if busy. Log the result to the journal, and surface "software update available/applied" on the wizard's System Check screen instead of failing silently (see S2 in my review above — silent failure is already a pattern to break).
5. Make it opt-out, not mandatory: a config key like `AUTO_UPDATE_SOFTWARE="true"` in `config.env`, shown in the wizard, so field users on metered connections can disable it.

This closes the loop for fixes like the ones in my review above: today, even after M1–M3 are fixed in this repo, already-provisioned Pis would never receive them unless each owner manually re-runs the installer.

— Bingo
