# Raspberry Pi Backend

## Setup

```bash
cd backend/api
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Set `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`, `CAMERA_URL`, and `CORS_ORIGINS` as needed. The API starts even when the broker is temporarily offline, and reports that state at `/api/status`.

Endpoints include `GET /api/status`, `POST /api/command`, and `POST /api/target`.
