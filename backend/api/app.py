import os
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import paho.mqtt.client as mqtt
import serial
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
SERIAL_PORT = os.getenv("ESP32_SERIAL_PORT", "auto")
SERIAL_BAUD = int(os.getenv("ESP32_SERIAL_BAUD", "115200"))
SERIAL_CONFIG_PATH = Path(os.getenv("ESP32_SERIAL_CONFIG", "/var/lib/astrodrive/serial-config.json"))
CAMERA_URL = os.getenv("CAMERA_URL", "http://localhost:8080/?action=stream")
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_connected = False
serial_connection = None
serial_lock = threading.Lock()
serial_port_name = SERIAL_PORT


def configured_serial_port() -> str:
    try:
        return json.loads(SERIAL_CONFIG_PATH.read_text()).get("port", SERIAL_PORT)
    except (OSError, json.JSONDecodeError):
        return SERIAL_PORT


def connect_serial(port_name: str):
    candidates = [port_name] if port_name != "auto" else [
        "/dev/ttyUSB0",
        "/dev/ttyACM0",
        "/dev/ttyUSB1",
        "/dev/ttyACM1",
    ]
    for port in candidates:
        try:
            return serial.Serial(port, SERIAL_BAUD, timeout=1)
        except (serial.SerialException, FileNotFoundError):
            continue
    return None


class Command(BaseModel):
    command: Literal["enable", "disable", "stop", "move"]
    axis: Literal["ra", "dec"] | None = None
    direction: Literal["forward", "backward"] | None = None
    steps: int = Field(default=0, ge=0, le=100000)


class Target(BaseModel):
    right_ascension: float = Field(ge=0, lt=24)
    declination: float = Field(ge=-90, le=90)


class SerialSettings(BaseModel):
    port: str = "auto"


def publish(payload: dict) -> None:
    delivered = False
    if mqtt_connected:
        result = mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))
        delivered = result.rc == mqtt.MQTT_ERR_SUCCESS
    if serial_connection is not None:
        command = payload["command"]
        if command in {"enable", "disable", "stop", "status"}:
            serial_command = command
        elif command == "move":
            serial_command = f"move {payload['axis']} {payload['direction']} {payload['steps']}"
        else:
            serial_command = ""
        if serial_command:
            with serial_lock:
                serial_connection.write(f"{serial_command}\n".encode("ascii"))
            delivered = True
    if not delivered:
        raise HTTPException(status_code=503, detail="MQTT broker and ESP32 are unavailable")


@app.on_event("startup")
def connect_mqtt() -> None:
    global mqtt_connected, serial_connection, serial_port_name
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
        mqtt_connected = True
    except OSError:
        mqtt_connected = False
    serial_port_name = configured_serial_port()
    serial_connection = connect_serial(serial_port_name)


@app.on_event("shutdown")
def disconnect_mqtt() -> None:
    global serial_connection
    mqtt_client.loop_stop()
    if mqtt_connected:
        mqtt_client.disconnect()
    if serial_connection is not None:
        serial_connection.close()
        serial_connection = None


@app.get("/api/status")
def status() -> dict:
    return {
        "connected": mqtt_connected,
        "esp32_connected": serial_connection is not None,
        "serial_port": serial_port_name,
        "mount": "idle",
        "camera_url": CAMERA_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.put("/api/settings/serial")
def update_serial_settings(request: SerialSettings) -> dict:
    global serial_connection, serial_port_name
    if request.port != "auto" and not request.port.startswith("/dev/"):
        raise HTTPException(status_code=422, detail="Serial port must be auto or a /dev path")
    if serial_connection is not None:
        serial_connection.close()
    serial_port_name = request.port
    SERIAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIAL_CONFIG_PATH.write_text(json.dumps({"port": serial_port_name}) + "\n")
    serial_connection = connect_serial(serial_port_name)
    return {"port": serial_port_name, "connected": serial_connection is not None}


@app.post("/api/command")
def command(request: Command) -> dict:
    publish(request.model_dump(exclude_none=True))
    return {"accepted": True, "command": request.command}


@app.post("/api/target")
def target(request: Target) -> dict:
    publish({"command": "goto", **request.model_dump()})
    return {"accepted": True, "target": request.model_dump()}
