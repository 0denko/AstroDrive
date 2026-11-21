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

1. **Silent firmware failure.** Step 4 runs `/opt/onstep-autoprovision/scripts/update_firmware.sh --all || true`, so if the OnStepX firmware download fails, the script still prints "AstroDrive is installed and ready!" The Setup Wizard's System Check may catch the missing firmware, but the success message alone can mislead a beginner. Suggestion: track the failure and print a clear warning in the final banner (e.g. "firmware download FAILED — connect the Pi to the internet and re-run setup.sh").

2. **Updates wipe local changes without warning.** On re-run, `git reset --hard origin/main` in `/opt/astrodrive-src` discards any local edits silently. Acceptable for an appliance-style install, but a one-line echo before the reset would set expectations.

Note: `setup.sh` is mostly a wrapper. A full review of the deployment path (`install.sh`, the firmware update script, the wizard backend, systemd/udev setup) is in progress; I will append those findings here when done.

— Bingo
