# Raspberry Pi Backend

## Setup

```bash
cd backend/api
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Connect the ESP32 to the Pi with a USB data cable. `ESP32_SERIAL_PORT=auto` tries common devices (`/dev/ttyUSB0`, `/dev/ttyACM0`, and their first alternates). The installer grants the API service access through the `dialout` group. If auto-detection does not find the board, run `ls /dev/ttyUSB* /dev/ttyACM*`, set the result in `/etc/astrodrive/astrodrive.env`, and restart with `sudo systemctl restart astrodrive-api`. Set `ESP32_SERIAL_BAUD=115200`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`, `CAMERA_URL`, and `CORS_ORIGINS` as needed. The API starts even when the broker is temporarily offline, and reports both links at `/api/status`.

The web UI sends commands to FastAPI. FastAPI forwards them over USB serial using `enable`, `disable`, `stop`, `move <ra|dec> <forward|backward> <steps>`, and `configure <RA step> <RA dir> <RA enable> <DEC step> <DEC dir> <DEC enable> <active-low>`. MQTT is an additional command publication path for other services. The serial port can be changed from the web UI; it is stored in `/var/lib/astrodrive/serial-config.json` and takes effect immediately. Driver settings are stored on both the Pi and ESP32.

Endpoints include `GET /api/status`, `POST /api/command`, and `POST /api/target`.
