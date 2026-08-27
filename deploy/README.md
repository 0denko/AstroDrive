# Ubuntu Deployment

## One-command install

The published repository is the default, so the Pi setup can be one command after Ubuntu is running:

```bash
curl -fsSL https://raw.githubusercontent.com/0denko/AstroDrive/main/deploy/install.sh | sudo bash
```

To deploy a fork, pass its repository URL after the script: `... | sudo bash -s -- https://github.com/OWNER/REPO`.

The installer installs Python, Node.js, Nginx, Git, and PlatformIO; clones the selected branch to `/opt/astrodrive`; creates the `astrodrive` service account; builds the API and UI; uploads changed ESP32 firmware when the board is connected, and enables the API plus Nginx at boot. It pauses the update timer during installation and uses a lock to prevent concurrent update runs. Its progress display stays at one consistent percentage sequence, including when it calls the updater internally.

Edit `/etc/astrodrive/astrodrive.env` for MQTT and camera settings, then run `sudo systemctl restart astrodrive-api`.

To run an update manually, use `sudo bash /opt/astrodrive/deploy/update.sh`. The service also invokes the updater through Bash, so the Git executable bit is not required.

The web UI's update button starts the root-owned systemd updater through a narrowly scoped sudo rule; it does not give the API general root access.

## Updates and restarts

`astrodrive-update.service` fetches the configured branch and exits immediately when it is already current. It refreshes the service, camera, and Nginx definitions and enablement on each update, installs missing webcam tools, and migrates the old localhost camera setting automatically. This lets it repair a missing deployment registration without rerunning the installer. It is enabled at boot, and the API is ordered after it without a circular dependency. When a commit is available, it updates only the affected layer: backend changes refresh Python dependencies, frontend changes rebuild the UI, and `esp32/` changes build/upload firmware. `astrodrive-update.timer` checks every 15 minutes, so a new commit is deployed without waiting for a reboot. Review `sudo journalctl -u astrodrive-update` when diagnosing an update. Set `ESP32_AUTO_FLASH=false` to disable automatic firmware uploads.

## Cloud-init

`cloud-init.yaml` is already configured for the public `0denko/AstroDrive` repository. For a cloud provider, paste its contents into the instance's **user-data** field. The server will install and deploy automatically on first boot.

For an Ubuntu Raspberry Pi image, create a NoCloud seed image before booting the SD card:

```bash
cloud-localds seed.img deploy/cloud-init.yaml deploy/no-cloud/meta-data
```

Mount or attach `seed.img` as the cloud-init seed device according to the image instructions. Copy `deploy/no-cloud/network-config.example` to `network-config` and add it to the `cloud-localds` command if the Pi needs a static network configuration. Cloud-init will run the installer once, then the AstroDrive update service will check GitHub on every boot and every 15 minutes.

For private repositories, use a deploy key or a short-lived credential configured by your provisioning system; do not put a token in a public cloud-init file or shell history.
