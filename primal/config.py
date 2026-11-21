import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path

DEFAULT_SERVER = "https://primal-space-monitor.vercel.app"

APP_DIR = Path(os.environ.get("PRIMAL_CONFIG_HOME", Path.home() / ".config" / "primal"))
STATE_DIR = Path(os.environ.get("PRIMAL_STATE_HOME", Path.home() / ".local" / "state" / "primal"))
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    "server_url": DEFAULT_SERVER,
    "device_name": None,
    "install_id": None,
    "device_id": None,
    "token": None,
    "enrollment_key": None,
    "compute_enabled": False,
    "wifi_only": True,
    "minimum_battery": 50,
    "maximum_cpu_usage": 20,
    "maximum_memory_usage_mb": 128,
    "worker_paused": False,
    "cancel_current": False,
    "heartbeat_interval": 30,
    "poll_interval": 5,
    "request_timeout": 20,
}

BOOL_KEYS = {
    "compute_enabled",
    "wifi_only",
    "worker_paused",
    "cancel_current",
}
INT_KEYS = {
    "minimum_battery",
    "maximum_cpu_usage",
    "maximum_memory_usage_mb",
    "heartbeat_interval",
    "poll_interval",
    "request_timeout",
}
SECRET_KEYS = {"token", "enrollment_key"}
TEXT_KEYS = {"server_url", "device_name"}


def ensure_dirs():
    for path in (APP_DIR, STATE_DIR):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def normalize_server(url):
    value = (url or "").strip().rstrip("/")
    if not value:
        raise ValueError("server URL is required")
    if not (value.startswith("https://") or value.startswith("http://localhost") or value.startswith("http://127.0.0.1")):
        raise ValueError("server URL must use https (http is allowed only for localhost)")
    return value


def load_config():
    ensure_dirs()
    config = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot read config {CONFIG_PATH}: {error}") from error
        if isinstance(stored, dict):
            config.update(stored)
    if not config.get("install_id"):
        config["install_id"] = str(uuid.uuid4())
        save_config(config)
    return config


def save_config(config):
    ensure_dirs()
    payload = json.dumps(config, indent=2, sort_keys=True) + "\n"
    fd, temp_path = tempfile.mkstemp(prefix="config.", dir=str(APP_DIR))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CONFIG_PATH)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def init_config(server=None, enrollment_key=None, name=None, force=False):
    ensure_dirs()
    if CONFIG_PATH.exists() and not force:
        config = load_config()
        if server:
            config["server_url"] = normalize_server(server)
        if enrollment_key:
            config["enrollment_key"] = enrollment_key.strip()
        if name:
            config["device_name"] = name.strip()
        save_config(config)
        return config

    config = dict(DEFAULTS)
    config["install_id"] = str(uuid.uuid4())
    config["server_url"] = normalize_server(server or DEFAULT_SERVER)
    if enrollment_key:
        config["enrollment_key"] = enrollment_key.strip()
    if name:
        config["device_name"] = name.strip()
    save_config(config)
    return config


def parse_value(key, raw):
    if key in BOOL_KEYS:
        value = str(raw).strip().lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"{key} expects true or false")
    if key in INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{key} expects an integer") from error
    if key == "server_url":
        return normalize_server(raw)
    if key == "device_name":
        return str(raw).strip()[:64]
    if key == "enrollment_key":
        return str(raw).strip()
    if key == "token":
        return str(raw).strip()
    raise KeyError(key)


def set_config_value(key, raw):
    allowed = BOOL_KEYS | INT_KEYS | TEXT_KEYS | {"token", "enrollment_key"}
    if key not in allowed:
        raise KeyError(key)
    value = parse_value(key, raw)
    config = load_config()
    config[key] = value
    save_config(config)
    return config


def masked(value):
    if not value:
        return "not set"
    text = str(value)
    if len(text) <= 8:
        return "set"
    return f"set ({text[:4]}…{text[-4:]})"


def new_install_id():
    return str(uuid.uuid4())


def random_seed():
    return secrets.token_hex(8)
