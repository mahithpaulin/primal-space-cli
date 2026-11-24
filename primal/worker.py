import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

from .api import ApiClient, ApiError
from .config import load_config, save_config
from .resources import battery_info, cpu_load_pct, cpu_usage_pct, network_info
from .lending import accrue, allowance_reached, lending_config, read_credits
from .wallet import accrue_wallet, apply_credit, cap_reached, read_wallet, roll_periods
from .sandbox import SandboxError, SandboxTimeout, run_http, run_python, run_shell, children_cpu_seconds
from .state import (
    inbox_path,
    message_cursor_path,
    read_worker_status,
    write_json,
    write_worker_status,
)
from .workload import (
    WORKLOADS,
    normalize_http_params,
    normalize_python_params,
    normalize_shell_params,
)

try:
    import resource
except ImportError:  # pragma: no cover - Android builds normally provide it
    resource = None

LOG = logging.getLogger("primal.worker")
STOP = threading.Event()


class TaskAborted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class TaskFailed(Exception):
    pass


class ResourceSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.cpu_samples = []
        self.rss_samples = []
        self.wall_start = time.monotonic()
        self.process_start = time.process_time()

    def run(self):
        while not self.stop_event.is_set():
            value = cpu_usage_pct(0.25)
            if value is not None:
                self.cpu_samples.append(value)
            rss = _rss_mb()
            if rss is not None:
                self.rss_samples.append(rss)
            if value is None:
                self.stop_event.wait(0.25)

    def finish(self):
        self.stop_event.set()
        self.join(timeout=2)
        wall = time.monotonic() - self.wall_start
        process_cpu = time.process_time() - self.process_start
        worker_cpu = round((process_cpu / wall) * 100, 1) if wall > 0 else None
        cpu_avg = round(sum(self.cpu_samples) / len(self.cpu_samples), 1) if self.cpu_samples else None
        rss_peak = round(max(self.rss_samples), 1) if self.rss_samples else None
        return cpu_avg, worker_cpu, cpu_load_pct(), rss_peak


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_status(fields=None, **kwargs):
    previous = read_worker_status()
    value = {**previous, **(fields or {}), **kwargs}
    write_worker_status(value)


def _rss_mb():
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    if resource is not None:
        try:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except (OSError, ValueError):
            pass
    return None


def _int_setting(value, fallback, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return max(minimum, min(maximum, number))


def eligibility(config):
    reasons = []
    network = network_info(config.get("server_url"))
    if network.get("network_connected") is False:
        reasons.append("network_offline")
    battery = battery_info()
    minimum = _int_setting(config.get("minimum_battery"), 20, 0, 100)
    percentage = battery.get("battery_pct")
    if percentage is None:
        if minimum > 0:
            reasons.append("battery_unavailable")
    elif percentage < minimum:
        reasons.append("battery_below_minimum")
    return reasons, network, battery


def _clear_cancel_flag():
    try:
        config = load_config()
        if config.get("cancel_current"):
            config["cancel_current"] = False
            save_config(config)
    except (OSError, RuntimeError):
        pass


def _remote_cancelled(client, task_id):
    try:
        payload = client.get_task(task_id)
    except ApiError:
        return False
    task = payload.get("task") or {}
    return task.get("status") == "cancelled" or task.get("cancel_requested") is True


def _submit(
    client,
    device_id,
    task_id,
    status,
    result=None,
    duration_ms=None,
    error=None,
    metrics=None,
    stdout=None,
):
    last_error = None
    for attempt in range(3):
        try:
            return client.submit_result(
                device_id,
                task_id,
                status,
                result=result,
                duration_ms=duration_ms,
                error=error,
                metrics=metrics,
                stdout=stdout,
            )
        except ApiError as api_error:
            last_error = api_error
            if STOP.is_set() and attempt >= 1:
                break
            time.sleep(2)
    raise last_error or ApiError(None, "could not submit result")


def execute_task(client, config, task):
    device_id = config["device_id"]
    task_id = task["id"]
    started = time.monotonic()
    cpu_started = time.process_time()
    child_cpu_started = children_cpu_seconds()
    started_at = _now()
    peak_rss = _rss_mb()
    _write_status({
            "status": "running",
            "current_task_id": task_id,
            "started_at": started_at,
            "compute_enabled": True,
            "last_error": None,
        }
    )
    LOG.info("task %s started", task_id)
    sampler = None

    try:
        workload = task.get("workload")
        if workload not in WORKLOADS:
            raise TaskFailed("unsupported_workload")
        limits = task.get("resource_limits") or {}
        lending = lending_config(config)
        cpu_limit = min(
            _int_setting(limits.get("max_cpu_pct"), 20, 1, 100),
            _int_setting(config.get("maximum_cpu_usage"), 20, 1, 100),
            lending["cpu_pct"] if lending["enabled"] else 100,
        )
        mem_limit = min(
            _int_setting(limits.get("max_memory_mb"), 128, 16, 4096),
            _int_setting(config.get("maximum_memory_usage_mb"), 128, 16, 4096),
        )
        deadline_seconds = _int_setting(limits.get("deadline_seconds"), 120, 5, 300)
        deadline = time.monotonic() + deadline_seconds

        try:
            os.nice(19)
        except OSError:
            pass

        sampler = ResourceSampler()
        sampler.start()
        result = None
        stdout = None

        params = task.get("params") or {}
        if workload == "python":
            normalized = normalize_python_params(params)
            max_seconds = _int_setting(limits.get("max_seconds"), 10, 1, 30)
            try:
                execution = run_python(
                    normalized["code"],
                    normalized["args"],
                    max_seconds=max_seconds,
                    memory_mb=mem_limit,
                    cpu_seconds=max(1, min(max_seconds, round(max_seconds * cpu_limit / 100))),
                )
            except SandboxTimeout as error:
                raise TaskFailed("deadline_exceeded") from error
            except SandboxError as error:
                raise TaskFailed(str(error)[:200]) from error
            if STOP.is_set():
                raise TaskAborted("worker_stopped")
            result = {"value": execution.get("result")}
            stdout = execution.get("stdout") or None
        elif workload == "shell":
            normalized = normalize_shell_params(params)
            max_seconds = _int_setting(limits.get("max_seconds"), 10, 1, 30)
            try:
                execution = run_shell(
                    normalized["command"],
                    normalized["items"],
                    shard_index=_int_setting(params.get("shard_index"), 0, 0, 64),
                    shard_count=_int_setting(params.get("shard_count"), 1, 1, 64),
                    max_seconds=max_seconds,
                    memory_mb=mem_limit,
                    cpu_seconds=max(1, min(max_seconds, round(max_seconds * cpu_limit / 100))),
                )
            except SandboxTimeout as error:
                raise TaskFailed("deadline_exceeded") from error
            except SandboxError as error:
                raise TaskFailed(str(error)[:200]) from error
            if STOP.is_set():
                raise TaskAborted("worker_stopped")
            result = {
                "value": {
                    "exit_code": execution["exit_code"],
                    "stdout": execution["stdout"],
                    "stderr": execution["stderr"],
                }
            }
            stdout = execution["stdout"] or None
        else:
            normalized = normalize_http_params(params)
            try:
                execution = run_http(normalized, timeout_s=normalized.get("timeout_s"))
            except SandboxError as error:
                raise TaskFailed(str(error)[:200]) from error
            if STOP.is_set():
                raise TaskAborted("worker_stopped")
            result = {
                "value": {
                    "status": execution["status"],
                    "body": execution["body"],
                    "truncated": execution["truncated"],
                }
            }
            stdout = None

        cpu_avg, worker_cpu, load_pct, rss_peak = sampler.finish()
        duration_ms = round((time.monotonic() - started) * 1000)
        cpu_ratio = None
        wall_seconds = max(0.001, time.monotonic() - started)
        parent_cpu = max(0.0, time.process_time() - cpu_started)
        child_cpu = None
        child_cpu_now = children_cpu_seconds()
        if child_cpu_started is not None and child_cpu_now is not None:
            child_cpu = max(0.0, child_cpu_now - child_cpu_started)
        cpu_seconds = max(parent_cpu, child_cpu or 0.0)
        cpu_ratio = min(1.0, cpu_seconds / wall_seconds)
        if lending["enabled"]:
            credits, earned = accrue(config, duration_ms / 1000.0)
            wallet, wallet_earned = accrue_wallet(config, duration_ms / 1000.0, cpu_ratio=cpu_ratio)
            if earned:
                LOG.info(
                    "earned %.4f compute units (balance %.4f CU, cpu ratio %s)",
                    earned,
                    credits.get("units", 0.0),
                    round(cpu_ratio, 3) if cpu_ratio is not None else "unknown",
                )
        if rss_peak is not None:
            peak_rss = max(peak_rss or 0, rss_peak)
        response = _submit(
            client,
            device_id,
            task_id,
            "completed",
            result=result,
            duration_ms=duration_ms,
            metrics={
                "cpu_pct_avg": cpu_avg,
                "cpu_load_pct": load_pct,
                "worker_cpu_pct_avg": worker_cpu,
                "rss_mb_peak": round(peak_rss, 1) if peak_rss is not None else None,
            },
            stdout=stdout,
        )
        verified = response.get("verified")
        _write_status({
                "status": "idle",
                "current_task_id": None,
                "last_error": None,
                "last_result": {
                    "task_id": task_id,
                    "status": "completed",
                    "value": result.get("value") if isinstance(result, dict) else result,
                    "verified": verified,
                    "duration_ms": duration_ms,
                    "completed_at": _now(),
                },
            }
        )
        LOG.info("task %s completed in %sms verified=%s", task_id, duration_ms, verified)
    except TaskFailed as failed:
        duration_ms = round((time.monotonic() - started) * 1000)
        message = str(failed)[:200]
        if lending_config(config)["enabled"]:
            accrue(config, duration_ms / 1000.0, earn_units=False)
        try:
            _submit(
                client,
                device_id,
                task_id,
                "failed",
                duration_ms=duration_ms,
                error=message,
            )
        except ApiError as error:
            LOG.error("could not report failure for %s: %s", task_id, error)
        _write_status({
                "status": "idle",
                "current_task_id": None,
                "last_error": message,
                "last_result": {
                    "task_id": task_id,
                    "status": "failed",
                    "value": None,
                    "verified": False,
                    "duration_ms": duration_ms,
                    "completed_at": _now(),
                },
            }
        )
        LOG.error("task %s failed: %s", task_id, message)
    except TaskAborted as aborted:
        duration_ms = round((time.monotonic() - started) * 1000)
        try:
            _submit(
                client,
                device_id,
                task_id,
                "cancelled",
                duration_ms=duration_ms,
                error=aborted.reason,
            )
        except ApiError as error:
            LOG.error("could not report cancellation for %s: %s", task_id, error)
        _write_status({
                "status": "idle",
                "current_task_id": None,
                "last_error": aborted.reason,
                "last_result": {
                    "task_id": task_id,
                    "status": "cancelled",
                    "value": None,
                    "verified": None,
                    "duration_ms": duration_ms,
                    "completed_at": _now(),
                },
            }
        )
        LOG.warning("task %s aborted: %s", task_id, aborted.reason)
    except Exception as error:  # noqa: BLE001 - report unexpected worker failures to server
        duration_ms = round((time.monotonic() - started) * 1000)
        message = f"{type(error).__name__}: {error}"[:200]
        try:
            _submit(
                client,
                device_id,
                task_id,
                "failed",
                duration_ms=duration_ms,
                error=message,
            )
        except ApiError as submit_error:
            LOG.error("could not report failure for %s: %s", task_id, submit_error)
        _write_status({
                "status": "idle",
                "current_task_id": None,
                "last_error": message,
                "last_result": {
                    "task_id": task_id,
                    "status": "failed",
                    "value": None,
                    "verified": False,
                    "duration_ms": duration_ms,
                    "completed_at": _now(),
                },
            }
        )
        LOG.error("task %s failed: %s", task_id, message)
    finally:
        if sampler is not None:
            sampler.stop_event.set()


def poll_messages(client, config):
    try:
        cursor = json.loads(message_cursor_path().read_text()).get("since")
    except (OSError, json.JSONDecodeError, AttributeError):
        cursor = None
    try:
        payload = client.get_messages(config["device_id"], cursor)
    except ApiError as error:
        LOG.debug("message poll failed: %s", error)
        return
    messages = payload.get("messages") or []
    if not messages:
        return
    newest = cursor
    try:
        with open(inbox_path(), "a", encoding="utf-8") as handle:
            for message in messages:
                LOG.info("message from %s: %s", message.get("from"), message.get("body"))
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
                credit = message.get("credit")
                if isinstance(credit, dict):
                    wallet, delta = apply_credit(read_wallet(), credit)
                    if delta:
                        LOG.info("wallet updated: %s %.4f CU (balance %.4f)", credit.get("kind"), delta, wallet.get("balance", 0.0))
                created = message.get("created_at")
                if created and (newest is None or created > newest):
                    newest = created
    except OSError as error:
        LOG.warning("could not write inbox: %s", error)
    if newest != cursor:
        try:
            write_json(message_cursor_path(), {"since": newest})
        except OSError as error:
            LOG.warning("could not save message cursor: %s", error)


def run_worker():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    def handle_signal(_signum, _frame):
        STOP.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    config = load_config()
    if not config.get("compute_enabled"):
        LOG.error("compute is disabled; run: primal compute enable")
        _write_status({"status": "disabled", "current_task_id": None, "last_error": "compute_disabled"})
        return
    if not config.get("device_id") or not config.get("token"):
        LOG.error("device is not registered; run: primal connect")
        _write_status({"status": "error", "current_task_id": None, "last_error": "not_registered"})
        return

    client = ApiClient(
        config["server_url"],
        token=config["token"],
        timeout=config.get("request_timeout", 20),
    )
    _write_status({
            "status": "idle",
            "current_task_id": None,
            "compute_enabled": True,
            "last_error": None,
        }
    )
    LOG.info("worker loop starting")

    while not STOP.is_set():
        config = load_config()
        mode = config.get("compute_mode") or "off"
        if mode == "off" or not config.get("compute_enabled"):
            _write_status({"status": "disabled", "current_task_id": None})
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue
        if config.get("worker_paused"):
            _write_status({"status": "paused", "current_task_id": None})
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue

        reasons, _network, _battery = eligibility(config)
        if reasons:
            _write_status({
                    "status": "idle",
                    "current_task_id": None,
                    "last_error": ",".join(reasons),
                }
            )
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue

        poll_messages(client, config)

        cap = cap_reached(config, roll_periods(read_wallet()))
        if cap:
            _write_status({
                    "status": "idle",
                    "current_task_id": None,
                    "last_error": cap,
                }
            )
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue

        if allowance_reached(config, read_credits()):
            _write_status({
                    "status": "idle",
                    "current_task_id": None,
                    "last_error": "lending_hourly_limit",
                }
            )
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue

        try:
            claim = client.claim(config["device_id"], True, list(WORKLOADS))
        except ApiError as error:
            LOG.warning("claim failed: %s", error)
            _write_status({
                    "status": "idle",
                    "current_task_id": None,
                    "last_error": f"claim_failed: {error}",
                }
            )
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue

        task = claim.get("task")
        if not task:
            _write_status({
                    "status": "idle",
                    "current_task_id": None,
                    "last_error": claim.get("reason"),
                }
            )
            STOP.wait(max(2, int(config.get("poll_interval", 5))))
            continue

        execute_task(client, config, task)

    _write_status({"status": "stopped", "current_task_id": None})
    LOG.info("worker loop stopped")
