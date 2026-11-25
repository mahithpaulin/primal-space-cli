import os
import platform
import subprocess

from . import __version__
from .config import load_config


def _run(command, timeout=5):
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
    return completed.stdout.strip()


def getprop(name):
    value = _run(["getprop", name])
    return value or None


def _desktop_default_name():
    return {
        "Windows": "Windows device",
        "Darwin": "macOS device",
        "Linux": "Linux device",
    }.get(platform.system(), "PrimalSpace device")


def cpu_cores():
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return len(affinity)
    except (AttributeError, OSError):
        pass
    return os.cpu_count()


def device_info(config=None):
    config = config or load_config()
    manufacturer = getprop("ro.product.manufacturer")
    model = getprop("ro.product.model")
    android_version = getprop("ro.build.version.release")
    sdk = getprop("ro.build.version.sdk")
    arch = getprop("ro.product.cpu.abi") or platform.machine() or None

    try:
        android_sdk = int(sdk) if sdk else None
    except ValueError:
        android_sdk = None

    if android_version or manufacturer or model:
        default_name = " ".join(part for part in (manufacturer, model) if part) or "Android device"
    else:
        manufacturer = platform.system() or "Desktop"
        model = platform.release() or None
        default_name = _desktop_default_name()
    name = (config.get("device_name") or default_name).strip()[:64]

    return {
        "name": name,
        "manufacturer": manufacturer,
        "model": model,
        "android_version": android_version,
        "android_sdk": android_sdk,
        "cpu_arch": arch,
        "cpu_cores": cpu_cores(),
        "cli_version": __version__,
    }
