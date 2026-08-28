import os
import re
import json
import time
import tempfile
import threading
import subprocess
from collections import deque
from glob import glob
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import paho.mqtt.client as mqtt
import serial
import requests
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time
import astropy.units as u
from skyfield.api import EarthSatellite, load, wgs84
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AfterValidator, BaseModel, Field

app = FastAPI(title="AstroDrive Mount API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[value.strip() for value in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)
UPDATE_SCRIPT = "/opt/astrodrive/deploy/update.sh"
UPDATE_UNIT = "astrodrive-update.service"
# update.sh prints "[ 40%] [####    ] Building web interface" between stages.
UPDATE_PROGRESS_PATTERN = re.compile(r"^\[\s*(\d{1,3})%\]\s*\[[#\s]*\]\s*(.*)$")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "telescope/mount/command")
SERIAL_PORT = os.getenv("ESP32_SERIAL_PORT", "auto")
SERIAL_BAUD = int(os.getenv("ESP32_SERIAL_BAUD", "115200"))
SERIAL_CONFIG_PATH = Path(os.getenv("ESP32_SERIAL_CONFIG", "/var/lib/astrodrive/serial-config.json"))
MOUNT_CONFIG_PATH = Path(os.getenv("MOUNT_CONFIG", "/var/lib/astrodrive/mount-config.json"))
CAMERA_URL = os.getenv("CAMERA_URL", "/camera/?action=stream")
CAMERA_SNAPSHOT_URL = os.getenv("CAMERA_SNAPSHOT_URL", "http://127.0.0.1:8080/?action=snapshot")
CAMERA_CAPTURE_PATH = Path(os.getenv("CAMERA_CAPTURE_PATH", "/var/lib/astrodrive/captures"))
STACK_OUTPUT_PATH = CAMERA_CAPTURE_PATH / "latest.jpg"
# nginx serves the captures directory, so the stacker log lives outside it
STACK_LOG_PATH = Path(tempfile.gettempdir()) / "astrodrive-stack.log"
# a continuous run would grow an appended log without bound, so progress is rewritten in place
STACK_STATUS_PATH = Path(tempfile.gettempdir()) / "astrodrive-stack.status"
# the stacker re-reads this every frame, which is how the stretch changes without a restart
STACK_TUNING_PATH = Path(tempfile.gettempdir()) / "astrodrive-stack.json"
stack_lock = threading.Lock()
stack_process: subprocess.Popen | None = None
stack_frames = {"count": 0, "stopped": False}
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mqtt_connected = False
serial_connection = None
serial_lock = threading.RLock()
serial_port_name = SERIAL_PORT
serial_probed_at = 0.0
# a missing board costs four failed opens, so status polls do not retry faster than this
SERIAL_PROBE_INTERVAL = 3.0
serial_log = deque(maxlen=400)
serial_log_lock = threading.Lock()
serial_log_seq = 0
serial_reader_started = False
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


def log_serial(direction: str, text: str) -> None:
    """Keeps a short transcript of the link so the UI can show what the board actually said."""
    global serial_log_seq
    with serial_log_lock:
        serial_log_seq += 1
        serial_log.append({
            "seq": serial_log_seq,
            "at": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "text": text,
        })


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


def refresh_serial() -> None:
    """The board is normally plugged in after the service starts, so probing only at startup left
    the link down until a restart. Status polls re-probe instead."""
    global serial_connection, serial_probed_at
    with serial_lock:
        if serial_connection is not None:
            # an unplugged adapter leaves the handle open but the device node disappears
            if Path(serial_connection.port).exists():
                return
            try:
                serial_connection.close()
            except OSError:
                pass
            serial_connection = None
        now = time.monotonic()
        if now - serial_probed_at < SERIAL_PROBE_INTERVAL:
            return
        serial_probed_at = now
        serial_connection = connect_serial(serial_port_name)


def serial_reader() -> None:
    while True:
        connection = serial_connection
        if connection is None:
            time.sleep(0.5)
            continue
        try:
            # the port carries a one second timeout, so this yields even on a silent board
            raw = connection.readline()
        except (OSError, AttributeError, TypeError):
            time.sleep(0.5)
            continue
        if raw:
            log_serial("rx", raw.decode("utf-8", "replace").strip())


def start_serial_reader() -> None:
    global serial_reader_started
    if serial_reader_started:
        return
    serial_reader_started = True
    threading.Thread(target=serial_reader, name="serial-reader", daemon=True).start()


def write_serial(command: str) -> bool:
    global serial_connection
    with serial_lock:
        if serial_connection is None:
            log_serial("error", f"{command} (no serial device open)")
            return False
        try:
            serial_connection.write(f"{command}\n".encode("ascii"))
            log_serial("tx", command)
            return True
        except OSError:
            try:
                serial_connection.close()
            except OSError:
                pass
            serial_connection = None
            log_serial("error", f"{command} (write failed, link dropped)")
            return False


def camera_device() -> str:
    configured = os.getenv("CAMERA_DEVICE", "auto")
    if configured != "auto":
        return configured
    streamer = subprocess.run(["pgrep", "-a", "mjpg_streamer"], capture_output=True, text=True, check=False)
    streaming = re.search(r"-d\s+(/dev/video\d+)", streamer.stdout)
    if streaming:
        # control the node that is actually streaming rather than whichever one glob returns first
        return streaming.group(1)
    # a Pi exposes codec and ISP nodes under /dev/video* too, so pick the first that can capture
    for path in sorted(glob("/dev/video*"), key=lambda name: int(re.sub(r"\D", "", name) or 0)):
        formats = subprocess.run(["v4l2-ctl", "--device", path, "--list-formats"], capture_output=True, text=True, check=False)
        if formats.returncode == 0 and "[0]" in formats.stdout:
            return path
    return "/dev/video0"


# "  exposure_time_absolute 0x009a0902 (int) : min=1 max=5000 step=1 default=157 value=157 flags=inactive"
CAMERA_CONTROL_PATTERN = re.compile(r"^\s*(\w+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.+)$")
# v4l2 renamed these controls; which spelling exists depends on the kernel version.
CAMERA_CONTROL_ALIASES = {
    "auto_exposure": ("auto_exposure", "exposure_auto"),
    "exposure": ("exposure_time_absolute", "exposure_absolute"),
    "gain": ("gain",),
    "brightness": ("brightness",),
    "contrast": ("contrast",),
    "saturation": ("saturation",),
    "auto_white_balance": ("white_balance_automatic", "white_balance_temperature_auto"),
    "white_balance_temperature": ("white_balance_temperature",),
    "auto_focus": ("focus_automatic_continuous", "focus_auto"),
    "focus": ("focus_absolute",),
}
# auto toggles come first: drivers mark the manual controls inactive while auto is engaged.
CAMERA_CONTROL_ORDER = ("auto_exposure", "auto_white_balance", "auto_focus", "exposure", "gain", "brightness", "contrast", "saturation", "white_balance_temperature", "focus")


def read_camera_controls(device: str) -> tuple[dict[str, dict], str]:
    try:
        result = subprocess.run(["v4l2-ctl", "--device", device, "--list-ctrls"], capture_output=True, text=True, check=False)
    except OSError as error:
        return {}, str(error)
    if result.returncode != 0:
        return {}, result.stderr.strip() or "Camera controls are unavailable"
    raw: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        match = CAMERA_CONTROL_PATTERN.match(line)
        if not match:
            continue
        name, kind, rest = match.groups()
        entry: dict = {"type": kind}
        for field in rest.split():
            key, separator, value = field.partition("=")
            if not separator:
                break
            entry[key] = int(value) if value.lstrip("-").isdigit() else value
        raw[name] = entry

    controls: dict[str, dict] = {}
    for field, aliases in CAMERA_CONTROL_ALIASES.items():
        name = next((alias for alias in aliases if alias in raw), "")
        if not name:
            continue
        entry = raw[name]
        controls[field] = {
            "name": name,
            "type": entry.get("type", "int"),
            "min": entry.get("min"),
            "max": entry.get("max"),
            "step": entry.get("step", 1),
            "default": entry.get("default"),
            "value": entry.get("value"),
            "inactive": entry.get("flags") == "inactive",
        }
    return controls, ""


def camera_control_values(controls: dict[str, dict]) -> dict:
    values: dict = {}
    for field, entry in controls.items():
        value = entry.get("value")
        if value is None:
            continue
        if field == "auto_exposure":
            # menu drivers use 1=manual and 3=auto, bool drivers use 0/1
            values[field] = value != 1 if entry["type"] == "menu" else bool(value)
        elif field in ("auto_white_balance", "auto_focus"):
            values[field] = bool(value)
        else:
            values[field] = value
    return values


def camera_control_argument(field: str, value, entry: dict) -> int:
    if field == "auto_exposure":
        return (3 if value else 1) if entry["type"] == "menu" else int(bool(value))
    if field in ("auto_white_balance", "auto_focus"):
        return int(bool(value))
    number = int(value)
    if entry["min"] is not None:
        number = max(number, entry["min"])
    if entry["max"] is not None:
        number = min(number, entry["max"])
    return number


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


class SerialCommand(BaseModel):
    # the firmware splits on newlines, so the charset stops one request becoming several commands
    command: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9 _.\-]+$")
    wait_seconds: float = Field(default=2.0, ge=0.1, le=10.0)


def _usable_pin(value: int) -> int:
    # An ESP32 and an ESP8266 both wire GPIO 6-11 to the SPI flash and 1/3 to the USB console.
    if 6 <= value <= 11:
        raise ValueError("GPIO 6-11 are wired to the SPI flash")
    if value in (1, 3):
        raise ValueError("GPIO 1 and 3 carry the USB serial console")
    return value


# 34-39 exist on an ESP32 but are input only, so they cannot drive a STEP, DIR or ENABLE line.
GpioPin = Annotated[int, Field(ge=0, le=33), AfterValidator(_usable_pin)]


class MountSettings(BaseModel):
    ra_steps_per_revolution: int = Field(ge=1, le=1000000)
    dec_steps_per_revolution: int = Field(ge=1, le=1000000)
    ra_belt_ratio: float = Field(gt=0, le=1000)
    dec_belt_ratio: float = Field(gt=0, le=1000)
    driver_type: Literal["step_dir"] = "step_dir"
    ra_step_pin: GpioPin
    ra_dir_pin: GpioPin
    ra_enable_pin: GpioPin
    dec_step_pin: GpioPin
    dec_dir_pin: GpioPin
    dec_enable_pin: GpioPin
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
    previous_invocation_id: str = ""


class UpdateStatus(BaseModel):
    state: Literal["idle", "running", "failed"]
    detail: str
    progress: int | None = None
    invocation_id: str = ""


class CameraControls(BaseModel):
    exposure: int | None = None
    gain: int | None = None
    brightness: int | None = None
    contrast: int | None = None
    saturation: int | None = None
    white_balance_temperature: int | None = None
    auto_exposure: bool | None = None
    auto_white_balance: bool | None = None
    auto_focus: bool | None = None
    focus: int | None = None


class StackTuning(BaseModel):
    # where the sky sits on the output scale; low keeps the night dark, high shows more faint detail
    background: float = Field(default=0.15, ge=0.02, le=0.6)
    # the fitted curve already sets the tone, so this is an extra bend and stays off by default
    gamma: float = Field(default=1.0, ge=1, le=5)


class StackRequest(StackTuning):
    # 0 keeps stacking until it is stopped, republishing the preview after every frame
    frames: int = Field(default=8, ge=0, le=600)
    interval_ms: int = Field(default=250, ge=0, le=10000)


class StackStatus(BaseModel):
    state: Literal["idle", "running", "complete", "failed"]
    detail: str = ""
    frames: int = 0
    image_url: str = ""
    completed_at: str = ""


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
            # the firmware reads this with sscanf %d, so a bare bool would arrive as "True" and be rejected
            serial_command = "configure {ra_step_pin} {ra_dir_pin} {ra_enable_pin} {dec_step_pin} {dec_dir_pin} {dec_enable_pin} {enable_active_low:d}".format(**payload)
        else:
            serial_command = ""
        if serial_command and write_serial(serial_command):
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
    start_serial_reader()
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
    refresh_serial()
    location = {key: mount_config[key] for key in ("latitude", "longitude", "elevation_m", "location_source")}
    return {
        "connected": mqtt_connected,
        "esp32_connected": serial_connection is not None,
        "serial_port": serial_port_name,
        "serial_device": serial_connection.port if serial_connection is not None else "",
        "mount": "tracking" if mount_config["tracking"] else "aligned" if mount_config["alignment"]["state"] == "complete" else "not_aligned",
        "mount_config": {key: mount_config[key] for key in ("ra_steps_per_revolution", "dec_steps_per_revolution", "ra_belt_ratio", "dec_belt_ratio", "driver_type", "ra_step_pin", "ra_dir_pin", "ra_enable_pin", "dec_step_pin", "dec_dir_pin", "dec_enable_pin", "enable_active_low")},
        "location": location,
        "alignment": mount_config["alignment"],
        "tracking": mount_config["tracking"],
        "camera_url": CAMERA_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _update_unit_properties() -> dict[str, str]:
    names = ("LoadState", "ActiveState", "SubState", "Result", "InvocationID")
    result = subprocess.run(
        ["systemctl", "show", UPDATE_UNIT, *(f"--property={name}" for name in names)],
        capture_output=True,
        text=True,
        check=False,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _update_unit_log(invocation_id: str) -> list[str]:
    if not invocation_id:
        return []
    result = subprocess.run(
        ["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation_id}", "-n", "80", "-o", "cat", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@app.post("/api/update", response_model=UpdateResult)
def trigger_update() -> UpdateResult:
    if not Path(UPDATE_SCRIPT).exists():
        raise HTTPException(status_code=503, detail="Updater is not installed")
    try:
        previous_invocation_id = _update_unit_properties().get("InvocationID", "")
        result = subprocess.run(["sudo", "-n", "systemctl", "start", "--no-block", UPDATE_UNIT], capture_output=True, text=True, check=False)
    except OSError as error:
        raise HTTPException(status_code=503, detail="Could not start updater") from error
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail=result.stderr.strip() or "Updater permission is not installed")
    return UpdateResult(started=True, message="Update started", previous_invocation_id=previous_invocation_id)


@app.get("/api/update/status", response_model=UpdateStatus)
def update_status() -> UpdateStatus:
    try:
        properties = _update_unit_properties()
        invocation_id = properties.get("InvocationID", "")
        lines = _update_unit_log(invocation_id)
    except OSError as error:
        return UpdateStatus(state="failed", detail=str(error))

    if properties.get("LoadState", "not-found") == "not-found":
        return UpdateStatus(state="failed", detail="Updater service is not installed")

    progress = None
    stage = ""
    plain: list[str] = []
    for line in lines:
        match = UPDATE_PROGRESS_PATTERN.match(line)
        if match:
            progress = int(match.group(1))
            stage = match.group(2).strip()
        else:
            plain.append(line)
    last_line = plain[-1] if plain else ""

    active_state = properties.get("ActiveState", "")
    sub_state = properties.get("SubState", "")
    # A Type=oneshot unit stays in "activating" for the whole run; it is never "active".
    if active_state == "activating" or (active_state == "active" and sub_state != "exited"):
        return UpdateStatus(state="running", detail=stage or last_line or "Update is in progress", progress=progress, invocation_id=invocation_id)
    if active_state == "failed" or properties.get("Result", "success") not in ("success", ""):
        return UpdateStatus(state="failed", detail=last_line or "Update failed", progress=progress, invocation_id=invocation_id)
    # a finished run ends on whatever the last stage happened to print, which is not a status
    if progress == 100:
        return UpdateStatus(state="idle", detail="Update finished", progress=progress, invocation_id=invocation_id)
    return UpdateStatus(state="idle", detail=last_line or "No update is running", progress=progress, invocation_id=invocation_id)


@app.get("/api/camera/controls")
def camera_controls() -> dict:
    device = camera_device()
    controls, error = read_camera_controls(device)
    return {"available": bool(controls), "device": device, "controls": controls, "values": camera_control_values(controls), "error": error}


@app.put("/api/camera/controls")
def set_camera_controls(request: CameraControls) -> dict:
    device = camera_device()
    controls, error = read_camera_controls(device)
    if not controls:
        raise HTTPException(status_code=503, detail=error or "Camera controls are unavailable")
    requested = request.model_dump(exclude_none=True)
    if not requested:
        raise HTTPException(status_code=400, detail="Choose at least one camera control")

    applied: dict = {}
    unsupported: list[str] = []
    failures: list[str] = []
    for field in CAMERA_CONTROL_ORDER:
        if field not in requested:
            continue
        entry = controls.get(field)
        if entry is None:
            unsupported.append(field)
            continue
        value = camera_control_argument(field, requested[field], entry)
        # one control per call so an unsupported value cannot discard the whole batch
        result = subprocess.run(["v4l2-ctl", "--device", device, "--set-ctrl", f"{entry['name']}={value}"], capture_output=True, text=True, check=False)
        if result.returncode != 0 or "error" in result.stdout.lower():
            failures.append(f"{field}: {(result.stderr or result.stdout).strip() or 'rejected by the driver'}")
        else:
            applied[field] = value
    if not applied and failures:
        raise HTTPException(status_code=422, detail="; ".join(failures))

    controls, _ = read_camera_controls(device)
    return {"applied": applied, "unsupported": unsupported, "failures": failures, "controls": controls, "updated_controls": camera_control_values(controls)}


def write_stack_tuning(tuning: StackTuning) -> None:
    staging = STACK_TUNING_PATH.with_suffix(".partial")
    staging.write_text(json.dumps({"background": tuning.background, "gamma": tuning.gamma}))
    # swap in one step so a stacker reading between frames never sees half a file
    staging.replace(STACK_TUNING_PATH)


@app.post("/api/camera/stack", response_model=StackStatus)
def stack_camera(request: StackRequest) -> StackStatus:
    global stack_process
    CAMERA_CAPTURE_PATH.mkdir(parents=True, exist_ok=True)
    with stack_lock:
        if stack_process is not None and stack_process.poll() is None:
            raise HTTPException(status_code=409, detail="A stack capture is already running")
        log = STACK_LOG_PATH.open("w")
        STACK_STATUS_PATH.write_text("Waiting for the first frame")
        write_stack_tuning(request)
        try:
            stack_process = subprocess.Popen(
                ["/usr/bin/python3", "/opt/astrodrive/deploy/stack-camera.py", str(STACK_OUTPUT_PATH), CAMERA_SNAPSHOT_URL, str(request.frames), str(request.interval_ms), str(STACK_TUNING_PATH), str(STACK_STATUS_PATH)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        except OSError as error:
            raise HTTPException(status_code=503, detail=f"Could not start the stacker: {error}") from error
        finally:
            log.close()
        stack_frames["count"] = request.frames
        stack_frames["stopped"] = False
    detail = "Stacking until stopped" if request.frames == 0 else f"Stacking {request.frames} frames"
    return StackStatus(state="running", detail=detail, frames=request.frames)


@app.put("/api/camera/stack/tuning", response_model=StackStatus)
def stack_tuning(request: StackTuning) -> StackStatus:
    write_stack_tuning(request)
    return stack_status()


@app.post("/api/camera/stack/stop", response_model=StackStatus)
def stack_stop() -> StackStatus:
    with stack_lock:
        process = stack_process
        stack_frames["stopped"] = True
        if process is not None and process.poll() is None:
            # the frames already averaged stay on disk, so stopping keeps the preview it has built
            process.terminate()
        # an API restart during a live stack drops the handle while the stacker keeps running
        elif subprocess.run(["pkill", "-f", "stack-camera.py"], check=False).returncode != 0:
            raise HTTPException(status_code=409, detail="No stack capture is running")
    return stack_status()


def stack_progress() -> str:
    return STACK_STATUS_PATH.read_text(errors="replace").strip() if STACK_STATUS_PATH.exists() else ""


@app.get("/api/camera/stack", response_model=StackStatus)
def stack_status() -> StackStatus:
    with stack_lock:
        process = stack_process
        frames = stack_frames["count"]
        stopped = stack_frames["stopped"]
    code = process.poll() if process is not None else None
    live = STACK_OUTPUT_PATH.exists()
    modified = STACK_OUTPUT_PATH.stat().st_mtime if live else 0
    if process is not None and code is None:
        return StackStatus(
            state="running",
            detail=stack_progress() or f"Stacking {frames} frames",
            frames=frames,
            # the stacker republishes after every frame, so the preview is watchable while it runs
            image_url=f"/captures/latest.jpg?t={int(modified)}" if live else "",
        )
    if process is not None and code != 0 and not stopped:
        log = STACK_LOG_PATH.read_text(errors="replace").strip().splitlines() if STACK_LOG_PATH.exists() else []
        return StackStatus(state="failed", detail=log[-1] if log else f"Stacker exited with code {code}", frames=frames)
    if not live:
        return StackStatus(state="idle", detail="No stacked frame captured yet")
    return StackStatus(
        state="complete",
        # the stacker reports the input signal level, which explains a frame that stays dark
        detail=stack_progress() or "Stacked frame ready",
        frames=frames,
        image_url=f"/captures/latest.jpg?t={int(modified)}",
        completed_at=datetime.fromtimestamp(modified, timezone.utc).isoformat(),
    )


@app.put("/api/settings/serial")
def update_serial_settings(request: SerialSettings) -> dict:
    global serial_connection, serial_port_name, serial_probed_at
    if request.port != "auto" and not request.port.startswith("/dev/"):
        raise HTTPException(status_code=422, detail="Serial port must be auto or a /dev path")
    with serial_lock:
        if serial_connection is not None:
            serial_connection.close()
        serial_port_name = request.port
        SERIAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SERIAL_CONFIG_PATH.write_text(json.dumps({"port": serial_port_name}) + "\n")
        serial_connection = connect_serial(serial_port_name)
        serial_probed_at = time.monotonic()
        device = serial_connection.port if serial_connection is not None else ""
    return {"port": serial_port_name, "device": device, "connected": serial_connection is not None}


@app.put("/api/settings/mount")
def update_mount_settings(request: MountSettings) -> dict:
    mount_config.update(request.model_dump())
    save_mount_config()
    publish({"command": "configure", **request.model_dump()})
    return {"mount_config": request.model_dump()}


@app.get("/api/serial/log")
def read_serial_log(after: int = 0) -> dict:
    refresh_serial()
    with serial_log_lock:
        entries = [entry for entry in serial_log if entry["seq"] > after]
        last_seq = serial_log_seq
    return {
        "entries": entries,
        "last_seq": last_seq,
        "connected": serial_connection is not None,
        "device": serial_connection.port if serial_connection is not None else "",
    }


@app.post("/api/serial/command")
def send_serial_command(request: SerialCommand) -> dict:
    refresh_serial()
    with serial_log_lock:
        start = serial_log_seq
    if not write_serial(request.command):
        raise HTTPException(status_code=503, detail="No serial device is open")
    deadline = time.monotonic() + request.wait_seconds
    while time.monotonic() < deadline:
        with serial_log_lock:
            replies = [entry for entry in serial_log if entry["seq"] > start and entry["direction"] == "rx"]
        if replies:
            return {"sent": request.command, "replies": replies}
        time.sleep(0.05)
    return {"sent": request.command, "replies": []}


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
