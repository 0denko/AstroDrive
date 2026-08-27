# Raspberry Pi Backend

## Setup

```bash
cd backend/api
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Connect the ESP32 to the Pi with a USB data cable. Set `ESP32_SERIAL_PORT` to the detected device (`/dev/ttyUSB0` or `/dev/ttyACM0`) and keep `ESP32_SERIAL_BAUD=115200`. The installer grants the API service access through the `dialout` group. Set `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`, `CAMERA_URL`, and `CORS_ORIGINS` as needed. The API starts even when the broker is temporarily offline, and reports both links at `/api/status`.

The web UI sends commands to FastAPI. FastAPI forwards them over USB serial using `enable`, `disable`, `stop`, and `move <ra|dec> <forward|backward> <steps>`. MQTT is an additional command publication path for other services.

Endpoints include `GET /api/status`, `POST /api/command`, and `POST /api/target`.
