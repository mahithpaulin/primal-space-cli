import json
import time
from datetime import datetime, timezone

from .state import STATE_DIR, ensure_dirs

CREDITS_FILE = STATE_DIR / "credits.json"

DEFAULT_CPU_PCT = 20
DEFAULT_MINUTES_PER_HOUR = 30
DEFAULT_UNITS_PER_HOUR = 1.0
HOUR_SECONDS = 3600


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def lending_config(config):
    return {
        "enabled": bool(config.get("lending_enabled")),
        "cpu_pct": int(config.get("lend_cpu_pct") or DEFAULT_CPU_PCT),
        "minutes_per_hour": int(config.get("lend_minutes_per_hour") or DEFAULT_MINUTES_PER_HOUR),
        "units_per_hour": float(config.get("lend_units_per_hour") or DEFAULT_UNITS_PER_HOUR),
    }


def read_credits():
    try:
        value = json.loads(CREDITS_FILE.read_text())
        if isinstance(value, dict):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "units": 0.0,
        "total_units": 0.0,
        "minutes_used": 0.0,
        "window_start": None,
        "updated_at": None,
        "history": {},
    }


def write_credits(value):
    ensure_dirs()
    temp = CREDITS_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(CREDITS_FILE)


def hour_window(now=None):
    stamp = int(now if now is not None else time.time())
    return stamp - (stamp % HOUR_SECONDS)


def minutes_used_this_hour(credits, now=None):
    window = hour_window(now)
    stored = credits.get("window_start")
    if stored != window:
        return 0.0
    try:
        return float(credits.get("minutes_used") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def allowance_reached(config, credits, now=None):
    settings = lending_config(config)
    if not settings["enabled"]:
        return False
    return minutes_used_this_hour(credits, now) >= settings["minutes_per_hour"]


def accrue(config, duration_seconds, now=None):
    """Add compute units for compute actually lent, and track the hourly allowance."""
    settings = lending_config(config)
    credits = read_credits()
    window = hour_window(now)
    if credits.get("window_start") != window:
        credits["window_start"] = window
        credits["minutes_used"] = 0.0

    duration = max(0.0, float(duration_seconds or 0.0))
    minutes = duration / 60.0
    credits["minutes_used"] = round(float(credits.get("minutes_used") or 0.0) + minutes, 4)
    credits["minutes_used"] = min(credits["minutes_used"], 60.0)

    earned = (duration / HOUR_SECONDS) * settings["units_per_hour"]
    if earned <= 0:
        return credits, 0.0

    credits["units"] = round(float(credits.get("units") or 0.0) + earned, 6)
    credits["total_units"] = round(float(credits.get("total_units") or 0.0) + earned, 6)
    credits["updated_at"] = _now_iso()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history = credits.get("history")
    if not isinstance(history, dict):
        history = {}
    history[day] = round(float(history.get(day) or 0.0) + earned, 6)
    credits["history"] = dict(sorted(history.items())[-30:])
    write_credits(credits)
    return credits, earned


def credits_payload(credits=None):
    value = credits if isinstance(credits, dict) else read_credits()
    return {
        "units": round(float(value.get("units") or 0.0), 6),
        "total_units": round(float(value.get("total_units") or 0.0), 6),
        "minutes_used": round(float(value.get("minutes_used") or 0.0), 4),
        "window_start": value.get("window_start"),
        "updated_at": value.get("updated_at"),
    }
