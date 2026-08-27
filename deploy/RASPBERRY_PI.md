# Raspberry Pi: Blank Card to AstroDrive

## Shortest reliable setup

1. Use Raspberry Pi Imager to flash **Ubuntu Server 24.04 LTS 64-bit** to the SD card. In the Imager settings, configure Wi-Fi, hostname, a user, and SSH public-key login.
2. Boot the Pi and find its address from the router or hostname:

   ```bash
   ssh ubuntu@astrodrive.local
   ```

3. Run the one-line installer:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/0denko/AstroDrive/main/deploy/install.sh | sudo bash
   ```

4. Open `http://astrodrive.local` from a computer on the same network.

The installer installs dependencies, builds the application, starts the API and Nginx, and enables update checks on boot and every 15 minutes. Set MQTT and camera values in `/etc/astrodrive/astrodrive.env` after installation.

## Fully unattended first boot

Use `deploy/cloud-init.yaml` as user-data with an Ubuntu cloud image or a NoCloud seed. This is suitable when the image already supports cloud-init. A normal interactive SD-card install is simpler with Raspberry Pi Imager followed by the one-line command above.

## Hardware warning

Test with motors disconnected first. Verify driver current limits, step/dir polarity, enable behavior, mechanical limits, and an emergency stop before attaching the telescope.