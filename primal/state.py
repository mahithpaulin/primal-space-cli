import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_DIR, ensure_dirs

IS_WINDOWS = sys.platform == "win32"
STILL_ACTIVE = 259


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(name, suffix):
    return STATE_DIR / f"{name}.{suffix}"


def pid_path(name):
    return _path(name, "pid")


def log_path(name):
    return _path(name, "log")


def runtime_path():
    return STATE_DIR / "runtime.json"


def worker_status_path():
    return STATE_DIR / "worker_status.json"


def message_cursor_path():
    return STATE_DIR / "messages.json"


def inbox_path():
    return STATE_DIR / "inbox.jsonl"


def _read_pid_file(name):
    try:
        return int(pid_path(name).read_text().strip())
    except (OSError, ValueError):
        return None


def _windows_pid_alive(pid):
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return False


def pid_alive(pid):
    if not pid or pid <= 1:
        return False
    if IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
        if b"primal" not in cmdline:
            return False
    except OSError:
        pass
    return True


def running_pid(name):
    pid = _read_pid_file(name)
    if pid and pid_alive(pid):
        return pid
    if pid:
        remove_pid(name)
    return None


def write_pid(name, pid):
    ensure_dirs()
    pid_path(name).write_text(f"{pid}\n")
    try:
        pid_path(name).chmod(0o600)
    except OSError:
        pass


def remove_pid(name):
    try:
        pid_path(name).unlink()
    except OSError:
        pass


def start_daemon(name, argv):
    ensure_dirs()
    existing = running_pid(name)
    if existing:
        return existing
    log = open(log_path(name), "a", encoding="utf-8")
    log.write(f"\n--- {_now()} starting {' '.join(argv)} ---\n")
    log.flush()
    if getattr(sys, "frozen", False):
        command = [sys.executable, *argv]
    else:
        command = [sys.executable, "-m", "primal.cli", *argv]
    options = {}
    if IS_WINDOWS:
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(Path.home()),
        close_fds=True,
        **options,
    )
    write_pid(name, process.pid)
    time.sleep(1.2)
    if process.poll() is not None:
        remove_pid(name)
        raise RuntimeError(f"{name} exited immediately; see {log_path(name)}")
    return process.pid


def stop_daemon(name, timeout=20):
    pid = running_pid(name)
    if not pid:
        remove_pid(name)
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        remove_pid(name)
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            remove_pid(name)
            return pid
        time.sleep(0.1)
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass
    remove_pid(name)
    return pid


def read_json(path, default):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, value):
    ensure_dirs()
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    os.replace(temp, path)


def read_runtime():
    return read_json(runtime_path(), {})


def write_runtime(value):
    write_json(runtime_path(), value)


def read_worker_status():
    return read_json(worker_status_path(), {})


def write_worker_status(value):
    value = dict(value)
    value["updated_at"] = _now()
    write_json(worker_status_path(), value)


def tail_log(name, lines=20):
    try:
        content = log_path(name).read_text(errors="replace").splitlines()
    except OSError:
        return []
    return content[-lines:]
