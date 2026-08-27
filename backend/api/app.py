import os
import json
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import paho.mqtt.client as mqtt
import serial
import requests
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u
from skyfield.api import EarthSatellite, load, wgs84
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
UPDATE_SCRIPT = "/opt/astrodrive/deploy/update.sh"

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "telescope/mount/command")
SERIAL_PORT = os.getenv("ESP32_SERIAL_PORT", "auto")
SERIAL_BAUD = int(os.getenv("ESP32_SERIAL_BAUD", "115200"))
SERIAL_CONFIG_PATH = Path(os.getenv("ESP32_SERIAL_CONFIG", "/var/lib/astrodrive/serial-config.json"))
MOUNT_CONFIG_PATH = Path(os.getenv("MOUNT_CONFIG", "/var/lib/astrodrive/mount-config.json"))
CAMERA_URL = os.getenv("CAMERA_URL", "/camera/?action=stream")
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_connected = False
serial_connection = None
serial_lock = threading.Lock()
serial_port_name = SERIAL_PORT
mount_config = {
    "ra_steps_per_revolution": 3200,
    "dec_steps_per_revolution": 3200,
    "ra_belt_ratio": 1.0,
    "dec_belt_ratio": 1.0,
    "driver_type": "step_dir",
    "ra_step_pin": 25,
    "ra_dir_pin": 26,
    "ra_enable_pin": 27,
    "dec_step_pin": 14,
    "dec_dir_pin": 12,
    "dec_enable_pin": 13,
    "enable_active_low": True,
    "latitude": 0.0,
    "longitude": 0.0,
    "elevation_m": 0.0,
    "location_source": "manual",
    "alignment": {"state": "not_started", "points": []},
    "tracking": False,
}


def configured_serial_port() -> str:
    try:
        return json.loads(SERIAL_CONFIG_PATH.read_text()).get("port", SERIAL_PORT)
    except (OSError, json.JSONDecodeError):
        return SERIAL_PORT


def load_mount_config() -> None:
    global mount_config
    try:
        loaded = json.loads(MOUNT_CONFIG_PATH.read_text())
        mount_config.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass


def save_mount_config() -> None:
    MOUNT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MOUNT_CONFIG_PATH.write_text(json.dumps(mount_config, indent=2) + "\n")


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


class MountSettings(BaseModel):
    ra_steps_per_revolution: int = Field(ge=1, le=1000000)
    dec_steps_per_revolution: int = Field(ge=1, le=1000000)
    ra_belt_ratio: float = Field(gt=0, le=1000)
    dec_belt_ratio: float = Field(gt=0, le=1000)
    driver_type: Literal["step_dir"] = "step_dir"
    ra_step_pin: int = Field(ge=0, le=39)
    ra_dir_pin: int = Field(ge=0, le=39)
    ra_enable_pin: int = Field(ge=0, le=39)
    dec_step_pin: int = Field(ge=0, le=39)
    dec_dir_pin: int = Field(ge=0, le=39)
    dec_enable_pin: int = Field(ge=0, le=39)
    enable_active_low: bool = True


class LocationSettings(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    elevation_m: float = Field(ge=-500, le=10000)
    location_source: Literal["manual", "gps"] = "manual"


class AlignmentPoint(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    right_ascension: float = Field(ge=0, lt=24)
    declination: float = Field(ge=-90, le=90)


class AlignmentAction(BaseModel):
    action: Literal["start", "complete", "reset"]


class TrackingRequest(BaseModel):
    enabled: bool


class UpdateResult(BaseModel):
    started: bool
    message: str


class UpdateStatus(BaseModel):
    state: Literal["idle", "running", "failed"]
    detail: str


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
        elif command == "configure":
            serial_command = "configure {ra_step_pin} {ra_dir_pin} {ra_enable_pin} {dec_step_pin} {dec_dir_pin} {dec_enable_pin} {enable_active_low}".format(**payload)
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
    load_mount_config()


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
    location = {key: mount_config[key] for key in ("latitude", "longitude", "elevation_m", "location_source")}
    return {
        "connected": mqtt_connected,
        "esp32_connected": serial_connection is not None,
        "serial_port": serial_port_name,
        "mount": "tracking" if mount_config["tracking"] else "aligned" if mount_config["alignment"]["state"] == "complete" else "not_aligned",
        "mount_config": {key: mount_config[key] for key in ("ra_steps_per_revolution", "dec_steps_per_revolution", "ra_belt_ratio", "dec_belt_ratio", "driver_type", "ra_step_pin", "ra_dir_pin", "ra_enable_pin", "dec_step_pin", "dec_dir_pin", "dec_enable_pin", "enable_active_low")},
        "location": location,
        "alignment": mount_config["alignment"],
        "tracking": mount_config["tracking"],
        "camera_url": CAMERA_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/update", response_model=UpdateResult)
def trigger_update() -> UpdateResult:
    if not Path(UPDATE_SCRIPT).exists():
        raise HTTPException(status_code=503, detail="Updater is not installed")
    try:
        subprocess.Popen(["sudo", "-n", "systemctl", "start", "astrodrive-update.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        raise HTTPException(status_code=503, detail="Could not start updater") from error
    return UpdateResult(started=True, message="Update started; refresh status in a moment")


@app.get("/api/update/status", response_model=UpdateStatus)
def update_status() -> UpdateStatus:
    try:
        state = subprocess.run(["systemctl", "is-active", "astrodrive-update.service"], capture_output=True, text=True, check=False).stdout.strip()
        detail = subprocess.run(["journalctl", "-u", "astrodrive-update.service", "-n", "1", "-o", "cat", "--no-pager"], capture_output=True, text=True, check=False).stdout.strip()
    except OSError as error:
        return UpdateStatus(state="failed", detail=str(error))
    if state == "active":
        return UpdateStatus(state="running", detail=detail or "Update is in progress")
    if state == "failed":
        return UpdateStatus(state="failed", detail=detail or "Update failed")
    return UpdateStatus(state="idle", detail=detail or "No update is running")


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


@app.put("/api/settings/mount")
def update_mount_settings(request: MountSettings) -> dict:
    mount_config.update(request.model_dump())
    save_mount_config()
    publish({"command": "configure", **request.model_dump()})
    return {"mount_config": request.model_dump()}


@app.put("/api/settings/location")
def update_location(request: LocationSettings) -> dict:
    mount_config.update(request.model_dump())
    save_mount_config()
    return {"location": request.model_dump()}


@app.post("/api/alignment")
def alignment_action(request: AlignmentAction) -> dict:
    if request.action == "start":
        mount_config["alignment"] = {"state": "collecting", "points": []}
    elif request.action == "reset":
        mount_config["alignment"] = {"state": "not_started", "points": []}
        mount_config["tracking"] = False
    else:
        if len(mount_config["alignment"]["points"]) < 1:
            raise HTTPException(status_code=400, detail="Add at least one alignment point")
        mount_config["alignment"]["state"] = "complete"
    save_mount_config()
    return mount_config["alignment"]


@app.post("/api/alignment/point")
def add_alignment_point(request: AlignmentPoint) -> dict:
    if mount_config["alignment"]["state"] != "collecting":
        raise HTTPException(status_code=409, detail="Start alignment before adding points")
    point = request.model_dump()
    now = Time.now()
    location = EarthLocation(lat=mount_config["latitude"] * u.deg, lon=mount_config["longitude"] * u.deg, height=mount_config["elevation_m"] * u.m)
    apparent = SkyCoord(ra=request.right_ascension * 15 * u.deg, dec=request.declination * u.deg).transform_to(AltAz(obstime=now, location=location))
    point["altitude"] = round(float(apparent.alt.deg), 4)
    point["azimuth"] = round(float(apparent.az.deg), 4)
    mount_config["alignment"]["points"].append(point)
    save_mount_config()
    return point


@app.post("/api/tracking")
def tracking(request: TrackingRequest) -> dict:
    if request.enabled and mount_config["alignment"]["state"] != "complete":
        raise HTTPException(status_code=409, detail="Complete mount alignment before tracking")
    mount_config["tracking"] = request.enabled
    save_mount_config()
    if not request.enabled:
        publish({"command": "stop"})
    return {"tracking": request.enabled}


@app.post("/api/command")
def command(request: Command) -> dict:
    publish(request.model_dump(exclude_none=True))
    return {"accepted": True, "command": request.command}


@app.post("/api/target")
def target(request: Target) -> dict:
    if mount_config["alignment"]["state"] != "complete":
        raise HTTPException(status_code=409, detail="Complete mount alignment before Go-To")
    now = Time.now()
    location = EarthLocation(lat=mount_config["latitude"] * u.deg, lon=mount_config["longitude"] * u.deg, height=mount_config["elevation_m"] * u.m)
    apparent = SkyCoord(ra=request.right_ascension * 15 * u.deg, dec=request.declination * u.deg).transform_to(AltAz(obstime=now, location=location))
    target_data = {"command": "goto", **request.model_dump(), "altitude": float(apparent.alt.deg), "azimuth": float(apparent.az.deg), "timestamp": now.isot}
    publish(target_data)
    return {"accepted": True, "target": target_data}


@app.get("/api/objects/resolve")
def resolve_object(name: str) -> dict:
    try:
        coordinate = SkyCoord.from_name(name)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not resolve astronomical object: {error}") from error
    return {"name": name, "right_ascension": round(coordinate.ra.hour, 6), "declination": round(coordinate.dec.deg, 6), "frame": coordinate.frame.name}


@app.get("/api/objects/satellites")
def satellites(limit: int = 20) -> dict:
    try:
        response = requests.get("https://celestrak.org/NORAD/elements/gp.php?GROUP=visual&FORMAT=tle", timeout=10)
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(status_code=502, detail="Satellite data source unavailable") from error
    lines = [line.strip() for line in response.text.splitlines() if line.strip()]
    objects = []
    timescale = load.timescale()
    observer = wgs84.latlon(mount_config["latitude"], mount_config["longitude"], elevation_m=mount_config["elevation_m"])
    observation_time = timescale.now()
    for index in range(0, len(lines) - 2, 3):
        if lines[index + 1].startswith("1 ") and lines[index + 2].startswith("2 "):
            satellite = EarthSatellite(lines[index + 1], lines[index + 2], lines[index], timescale)
            altitude, azimuth, distance = (satellite - observer).at(observation_time).altaz()
            objects.append({"name": lines[index], "tle": [lines[index + 1], lines[index + 2]], "altitude": round(altitude.degrees, 2), "azimuth": round(azimuth.degrees, 2), "distance_km": round(distance.km, 1)})
        if len(objects) >= max(1, min(limit, 100)):
            break
    return {"source": "CelesTrak visual group", "objects": objects}
