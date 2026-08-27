import os
from datetime import datetime, timezone
from typing import Literal

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="AstroDrive Mount API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[value.strip() for value in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "telescope/mount/command")
CAMERA_URL = os.getenv("CAMERA_URL", "http://localhost:8080/?action=stream")
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_connected = False


class Command(BaseModel):
    command: Literal["enable", "disable", "stop", "move"]
    axis: Literal["ra", "dec"] | None = None
    direction: Literal["forward", "backward"] | None = None
    steps: int = Field(default=0, ge=0, le=100000)


class Target(BaseModel):
    right_ascension: float = Field(ge=0, lt=24)
    declination: float = Field(ge=-90, le=90)


def publish(payload: dict) -> None:
    if not mqtt_connected:
        raise HTTPException(status_code=503, detail="MQTT broker is unavailable")
    result = mqtt_client.publish(MQTT_TOPIC, str(payload))
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise HTTPException(status_code=502, detail="Could not publish mount command")


@app.on_event("startup")
def connect_mqtt() -> None:
    global mqtt_connected
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
        mqtt_connected = True
    except OSError:
        mqtt_connected = False


@app.on_event("shutdown")
def disconnect_mqtt() -> None:
    mqtt_client.loop_stop()
    if mqtt_connected:
        mqtt_client.disconnect()


@app.get("/api/status")
def status() -> dict:
    return {
        "connected": mqtt_connected,
        "mount": "idle",
        "camera_url": CAMERA_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/command")
def command(request: Command) -> dict:
    publish(request.model_dump(exclude_none=True))
    return {"accepted": True, "command": request.command}


@app.post("/api/target")
def target(request: Target) -> dict:
    publish({"command": "goto", **request.model_dump()})
    return {"accepted": True, "target": request.model_dump()}
