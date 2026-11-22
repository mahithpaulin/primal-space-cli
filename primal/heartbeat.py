import logging
import signal
import threading
import time

from .api import ApiClient, ApiError
from .config import load_config
from .device import device_info
from .resources import collect_metrics
from .state import (
    read_worker_status,
    running_pid,
    write_runtime,
)
from .lending import credits_payload, lending_config, read_credits
from .workload import WORKLOADS

LOG = logging.getLogger("primal.heartbeat")
STOP = threading.Event()


def _worker_payload(config):
    status = read_worker_status()
    if not running_pid("worker"):
        state = "stopped"
    elif not config.get("compute_enabled"):
        state = "disabled"
    elif config.get("worker_paused"):
        state = "paused"
    else:
        state = status.get("status") or "idle"
        if state not in {"starting", "idle", "running", "paused", "disabled", "stopped", "error"}:
            state = "idle"
    return {
        "status": state,
        "compute_enabled": bool(config.get("compute_enabled")),
        "paused": bool(config.get("worker_paused")),
        "current_task_id": status.get("current_task_id"),
        "updated_at": status.get("updated_at"),
        "last_error": status.get("last_error"),
        "last_result": status.get("last_result"),
    }


def _apply_lending_request(config, response):
    request = response.get("lending_request")
    if not isinstance(request, dict):
        return
    changed = False
    wanted = {
        "lending_enabled": bool(request.get("enabled")),
        "lend_cpu_pct": int(request.get("cpu_pct") or 20),
        "lend_minutes_per_hour": int(request.get("minutes_per_hour") or 30),
        "lend_units_per_hour": float(request.get("units_per_hour") or 1.0),
    }
    for key, value in wanted.items():
        if config.get(key) != value:
            config[key] = value
            changed = True
    if changed:
        save_config(config)
        LOG.info("applied operator lending request: %s", wanted)


def send_once(config=None, log=None):
    config = config or load_config()
    device_id = config.get("device_id")
    token = config.get("token")
    if not device_id or not token:
        raise RuntimeError("device is not registered; run: primal connect")
    client = ApiClient(config["server_url"], token=token, timeout=config.get("request_timeout", 20))
    info = device_info(config)
    metrics = collect_metrics(config.get("server_url"))
    worker = _worker_payload(config)
    response = client.heartbeat(
        device_id,
        metrics,
        worker,
        info["cli_version"],
        list(WORKLOADS),
        credits=credits_payload(read_credits()),
        lending=lending_config(config),
    )
    _apply_lending_request(config, response)
    write_runtime(
        {
            "last_heartbeat_ok": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_error": None,
            "server_time": response.get("server_time"),
            "status": response.get("status"),
        }
    )
    if log:
        log.info("heartbeat ok: %s", response.get("status"))
    return response, metrics, worker


def run_heartbeat(once=False):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    def handle_signal(_signum, _frame):
        STOP.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    LOG.info("heartbeat loop starting")
    while not STOP.is_set():
        config = load_config()
        try:
            send_once(config, LOG)
        except (ApiError, RuntimeError, OSError) as error:
            message = str(error)
            if isinstance(error, ApiError) and error.status == 401:
                message = "device token rejected; run: primal init --force && primal connect"
            LOG.error("heartbeat failed: %s", message)
            write_runtime(
                {
                    "last_heartbeat_ok": None,
                    "last_error": message,
                    "last_attempt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        if once:
            break
        STOP.wait(max(5, int(config.get("heartbeat_interval", 30))))
    LOG.info("heartbeat loop stopped")
