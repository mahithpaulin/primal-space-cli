import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .device import device_info

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("Reserved", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _windows_memory():
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        return {
            "MemTotal": int(status.ullTotalPhys // 1024),
            "MemAvailable": int(status.ullAvailPhys // 1024),
        }
    except (AttributeError, OSError, ValueError):
        return {}


def _filetime_value(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def _windows_cpu_times():
    try:
        idle = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return _filetime_value(kernel) + _filetime_value(user), _filetime_value(idle)
    except (AttributeError, OSError, ValueError):
        return None


def _sysctl_number(name):
    try:
        completed = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=3, check=False
        )
        if completed.returncode == 0:
            return int(completed.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _macos_ram():
    total = _sysctl_number("hw.memsize")
    if not total:
        return {}
    page_size = _sysctl_number("hw.pagesize") or 4096
    available = None
    try:
        completed = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=4, check=False
        )
        if completed.returncode == 0:
            pages = 0
            for line in completed.stdout.splitlines():
                label, _, rest = line.partition(":")
                number = rest.strip().rstrip(".").split()
                if not number or not number[0].isdigit():
                    continue
                if label.strip() in {"Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"}:
                    pages += int(number[0])
            available = pages * page_size
    except (OSError, subprocess.SubprocessError, ValueError):
        available = None
    info = {"MemTotal": int(total // 1024)}
    if available is not None:
        info["MemAvailable"] = int(available // 1024)
    return info


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
    if not info.get("MemTotal") and IS_WINDOWS:
        info = _windows_memory()
    if not info.get("MemTotal") and IS_MACOS:
        info = _macos_ram()
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
        fields = []
    if len(fields) >= 4:
        total = sum(fields)
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return total, idle
    if IS_WINDOWS:
        return _windows_cpu_times()
    return None


def _ps_cpu_usage():
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "%cpu="], capture_output=True, text=True, timeout=4, check=False
        )
        if completed.returncode != 0:
            return None
        total = sum(float(value) for value in completed.stdout.split() if value)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    cores = os.cpu_count() or 1
    return round(max(0.0, min(100.0, total / cores)), 1)


def cpu_usage_pct(sample_seconds=0.25):
    first = _cpu_times()
    if first is None:
        time.sleep(sample_seconds)
        if IS_MACOS:
            return _ps_cpu_usage()
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
        return float(os.getloadavg()[0])
    except (AttributeError, OSError):
        pass
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
    if battery_pct is not None or charging is not None:
        return {"battery_pct": battery_pct, "charging": charging}
    if IS_WINDOWS:
        return _windows_battery()
    if IS_MACOS:
        return _macos_battery()
    return {"battery_pct": None, "charging": None}


def _windows_battery():
    try:
        status = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return {"battery_pct": None, "charging": None}
    except (AttributeError, OSError, ValueError):
        return {"battery_pct": None, "charging": None}
    battery_pct = status.BatteryLifePercent
    if battery_pct == 255:
        battery_pct = None
    else:
        battery_pct = max(0, min(100, battery_pct))
    charging = None
    if status.ACLineStatus == 1:
        charging = True
    elif status.ACLineStatus == 0:
        charging = False
    return {"battery_pct": battery_pct, "charging": charging}


def _macos_battery():
    try:
        completed = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=4, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return {"battery_pct": None, "charging": None}
    if completed.returncode != 0:
        return {"battery_pct": None, "charging": None}
    battery_pct = None
    charging = None
    if "AC Power" in completed.stdout:
        charging = True
    elif "Battery Power" in completed.stdout:
        charging = False
    for token in completed.stdout.replace(";", " ").split():
        if token.endswith("%") and token[:-1].isdigit():
            battery_pct = max(0, min(100, int(token[:-1])))
            break
    return {"battery_pct": battery_pct, "charging": charging}


def _default_interface():
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "00000000":
            return fields[0]
    if IS_MACOS:
        try:
            completed = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            for line in completed.stdout.splitlines():
                if line.strip().startswith("interface:"):
                    return line.split(":", 1)[1].strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
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
    if IS_MACOS and interface.startswith("en"):
        return "wifi" if interface in {"en0", "en1"} else "ethernet"
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
