import hashlib
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone

from .api import ApiClient, ApiError
from .config import load_config, save_config
from .resources import battery_info, cpu_load_pct, cpu_usage_pct, network_info
from .state import read_worker_status, write_worker_status
from .workload import MOD, WORKLOAD, chunk_bounds, normalize_params

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
    if config.get("wifi_only") and network.get("network_type") != "wifi":
        reasons.append("wifi_required")

    battery = battery_info()
    minimum = _int_setting(config.get("minimum_battery"), 50, 0, 100)
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


def _compute_range(seed, start, end, cpu_limit, mem_limit, deadline):
    total = 0
    batch_start = time.monotonic()
    batch_iterations = 0

    for index in range(start, end):
        if STOP.is_set():
            raise TaskAborted("worker_stopped")

        if index % 1000 == 0:
            config = load_config()
            if not config.get("compute_enabled"):
                raise TaskAborted("compute_disabled")
            if config.get("cancel_current"):
                _clear_cancel_flag()
                raise TaskAborted("cancelled_by_user")
            if deadline is not None and time.monotonic() > deadline:
                raise TaskFailed("deadline_exceeded")
            rss = _rss_mb()
            if rss is not None and rss > mem_limit:
                raise TaskFailed("memory_limit_exceeded")

        digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
        total = (total + int.from_bytes(digest[:8], "big")) % MOD
        batch_iterations += 1

        if batch_iterations >= 1000:
            elapsed = time.monotonic() - batch_start
            if cpu_limit < 100 and elapsed > 0:
                time.sleep(elapsed * (100 - cpu_limit) / cpu_limit)
            batch_start = time.monotonic()
            batch_iterations = 0

    return total


def _submit(client, device_id, task_id, status, result=None, duration_ms=None, error=None, metrics=None):
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
        if task.get("workload") != WORKLOAD:
            raise TaskFailed("unsupported_workload")
        params = normalize_params(task.get("params"))
        limits = task.get("resource_limits") or {}
        cpu_limit = min(
            _int_setting(limits.get("max_cpu_pct"), 20, 1, 100),
            _int_setting(config.get("maximum_cpu_usage"), 20, 1, 100),
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

        seed = params["seed"]
        iterations = params["iterations"]
        chunks = params["chunks"]
        total = 0
        completed_chunks = 0

        for chunk_index, start, end in chunk_bounds(iterations, chunks):
            if STOP.is_set():
                raise TaskAborted("worker_stopped")
            if _remote_cancelled(client, task_id):
                raise TaskAborted("cancelled_by_operator")
            value = _compute_range(
                seed,
                start,
                end,
                cpu_limit,
                mem_limit,
                deadline,
            )
            total = (total + value) % MOD
            completed_chunks += 1
            LOG.info("task %s chunk %s/%s complete", task_id, completed_chunks, chunks)

        cpu_avg, worker_cpu, load_pct, rss_peak = sampler.finish()
        if rss_peak is not None:
            peak_rss = max(peak_rss or 0, rss_peak)
        duration_ms = round((time.monotonic() - started) * 1000)
        response = _submit(
            client,
            device_id,
            task_id,
            "completed",
            result={"value": total},
            duration_ms=duration_ms,
            metrics={
                "cpu_pct_avg": cpu_avg,
                "cpu_load_pct": load_pct,
                "worker_cpu_pct_avg": worker_cpu,
                "rss_mb_peak": round(peak_rss, 1) if peak_rss is not None else None,
            },
        )
        verified = response.get("verified")
        _write_status({
                "status": "idle",
                "current_task_id": None,
                "last_error": None,
                "last_result": {
                    "task_id": task_id,
                    "status": "completed",
                    "value": total,
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
        if not config.get("compute_enabled"):
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

        try:
            claim = client.claim(config["device_id"], True)
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
