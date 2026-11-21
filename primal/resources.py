import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .device import device_info


def _run_json(command, timeout=8):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _meminfo():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.strip().split()
            if parts:
                values[key] = int(parts[0])
    except (OSError, ValueError):
        return {}
    return values


def ram_info():
    info = _meminfo()
    total_kb = info.get("MemTotal")
    if not total_kb:
        return {"ram_total_mb": None, "ram_available_mb": None, "ram_used_pct": None}

    available_kb = info.get("MemAvailable")
    if available_kb is None:
        available_kb = (
            info.get("MemFree", 0)
            + info.get("Buffers", 0)
            + info.get("Cached", 0)
        )

    total_mb = round(total_kb / 1024)
    available_mb = max(0, min(round(available_kb / 1024), total_mb))
    used_pct = round(((total_mb - available_mb) / total_mb) * 100, 1) if total_mb else None
    return {
        "ram_total_mb": total_mb,
        "ram_available_mb": available_mb,
        "ram_used_pct": used_pct,
    }


def _cpu_times():
    try:
        line = Path("/proc/stat").read_text().splitlines()[0]
        fields = [int(value) for value in line.split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    if len(fields) < 4:
        return None
    total = sum(fields)
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return total, idle


def cpu_usage_pct(sample_seconds=0.25):
    first = _cpu_times()
    if first is None:
        time.sleep(sample_seconds)
        return None
    time.sleep(sample_seconds)
    second = _cpu_times()
    if second is None:
        return None
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    usage = 100.0 * (1.0 - (idle_delta / total_delta))
    return round(max(0.0, min(100.0, usage)), 1)


def load_average():
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        completed = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or "load average:" not in completed.stdout:
        return None
    try:
        return float(completed.stdout.split("load average:")[1].split(",")[0].strip())
    except (IndexError, ValueError):
        return None


def cpu_load_pct(cores=None):
    load = load_average()
    cores = cores or os.cpu_count()
    if load is None or not cores:
        return None
    return round((load / cores) * 100, 1)


def storage_free_mb(path=None):
    try:
        usage = shutil.disk_usage(path or Path.home())
    except OSError:
        return None
    return round(usage.free / (1024 * 1024))


def battery_info():
    status = _run_json(["termux-battery-status"])
    if status is not None:
        percentage = status.get("percentage")
        plugged = status.get("plugged")
        state = status.get("status")
        charging = None
        if isinstance(plugged, str):
            charging = plugged.upper() != "UNPLUGGED"
        elif isinstance(state, str):
            charging = state.upper() in {"CHARGING", "FULL"}
        try:
            battery_pct = int(percentage) if percentage is not None else None
        except (TypeError, ValueError):
            battery_pct = None
        if battery_pct is not None:
            battery_pct = max(0, min(100, battery_pct))
        return {"battery_pct": battery_pct, "charging": charging}

    battery_pct = None
    charging = None
    try:
        capacity = Path("/sys/class/power_supply/battery/capacity").read_text().strip()
        battery_pct = max(0, min(100, int(capacity)))
    except (OSError, ValueError):
        pass
    try:
        state = Path("/sys/class/power_supply/battery/status").read_text().strip().lower()
        if state in {"charging", "full"}:
            charging = True
        elif state in {"discharging", "not charging"}:
            charging = False
    except OSError:
        pass
    return {"battery_pct": battery_pct, "charging": charging}


def _default_interface():
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "00000000":
            return fields[0]
    return None


def _interface_type(interface):
    if not interface:
        return "unknown"
    if interface.startswith(("wlan", "swlan")):
        return "wifi"
    if interface.startswith(("rmnet", "ccmni", "pdp", "v4-")):
        return "cellular"
    if interface.startswith("eth"):
        return "ethernet"
    return "unknown"


def _wifi_connected():
    status = _run_json(["termux-wifi-connectioninfo"])
    if status is None:
        return None
    state = str(status.get("supplicant_state") or "").upper()
    ssid = status.get("ssid")
    if state == "COMPLETED":
        return True
    if ssid and ssid not in {"<unknown ssid>", "unknown"}:
        return True
    return False


def _tcp_reachable(server_url):
    parsed = urlparse(server_url or "")
    host = parsed.hostname
    if not host:
        return None
    port = 443 if parsed.scheme == "https" else 80
    try:
        with socket.create_connection((host, port), timeout=4):
            return True
    except OSError:
        return False


def network_info(server_url=None):
    wifi = _wifi_connected()
    interface = _default_interface()
    interface_type = _interface_type(interface)
    reachable = _tcp_reachable(server_url)

    if wifi is True:
        network_type = "wifi"
    elif wifi is False and interface_type == "cellular":
        network_type = "cellular"
    elif interface_type != "unknown":
        network_type = interface_type
    else:
        network_type = "unknown"

    connected = reachable
    if connected is None:
        connected = True if wifi is True else None
    if connected is False:
        network_type = "none"

    return {"network_connected": connected, "network_type": network_type}


def collect_metrics(server_url=None):
    metrics = {}
    metrics["cpu_usage_pct"] = cpu_usage_pct()
    metrics["cpu_load_pct"] = cpu_load_pct()
    metrics.update(ram_info())
    metrics["storage_free_mb"] = storage_free_mb()
    metrics.update(battery_info())
    metrics.update(network_info(server_url))
    return metrics


def discover(config):
    info = device_info(config)
    metrics = collect_metrics(config.get("server_url"))
    return {
        **info,
        "metrics": metrics,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
