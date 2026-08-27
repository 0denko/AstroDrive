# Raspberry Pi: Blank Card to AstroDrive

## Shortest reliable setup

1. Use Raspberry Pi Imager to flash **Raspberry Pi OS Lite (64-bit)** to the SD card. Find it under **Raspberry Pi OS > Raspberry Pi OS (other)**. In the Imager settings, configure Wi-Fi, hostname, a user, and SSH public-key login.
2. Boot the Pi and find its address from the router or hostname:

   ```bash
   ssh ubuntu@astrodrive.local
   ```

3. Run the one-line installer:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/0denko/AstroDrive/main/deploy/install.sh | sudo bash
   ```

4. Open `http://astrodrive.local` from a computer on the same network.

The installer installs dependencies, builds the application, starts the API and Nginx, and enables update checks on boot and every 15 minutes. Set MQTT and camera values in `/etc/astrodrive/astrodrive.env` after installation. Raspberry Pi OS Lite is recommended for the Pi 3's 1 GB of RAM; do not install the desktop image.

## Fully unattended first boot

Cloud-init is not the simplest route on a Pi 3 SD-card install because Raspberry Pi OS does not normally consume cloud-init user-data by default. Use Raspberry Pi Imager customization followed by the one-line command above. The cloud-init files remain available for Ubuntu cloud images and automated server provisioning.

## Hardware warning

Test with motors disconnected first. Verify driver current limits, step/dir polarity, enable behavior, mechanical limits, and an emergency stop before attaching the telescope.