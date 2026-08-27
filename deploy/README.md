# Ubuntu Deployment

## One-command install

Replace the repository URL with the real GitHub repository:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/AstroDrive/main/deploy/install.sh | sudo bash -s -- https://github.com/OWNER/AstroDrive.git
```

The installer installs Python, Node.js, Nginx, and Git; clones the selected branch to `/opt/astrodrive`; creates the `astrodrive` service account; builds the API and UI; and enables the API plus Nginx at boot.

Edit `/etc/astrodrive/astrodrive.env` for MQTT and camera settings, then run `sudo systemctl restart astrodrive-api`.

## Updates and restarts

`astrodrive-update.service` fetches the configured branch and rebuilds the application before the API starts. `astrodrive-update.timer` also checks every 15 minutes, so a new commit is deployed without waiting for a reboot. Review `sudo journalctl -u astrodrive-update` when diagnosing an update.

## Cloud-init

Edit `cloud-init.yaml` with the real repository URL and provide it as user-data when creating an Ubuntu server. The server will install and deploy automatically on first boot.

For private repositories, use a deploy key or a short-lived credential configured by your provisioning system; do not put a token in a public cloud-init file or shell history.
