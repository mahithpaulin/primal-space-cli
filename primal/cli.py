import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import __version__
from .api import ApiClient, ApiError
from .config import (
    CONFIG_PATH,
    DEFAULTS,
    load_config,
    masked,
    init_config,
    save_config,
    set_config_value,
)
from .device import device_info
from .heartbeat import run_heartbeat, send_once
from .wallet import (
    WALLET_FILE,
    caps as compute_caps,
    caps_payload,
    cap_reached,
    read_wallet,
    roll_periods,
    wallet_payload,
)
from .lending import (
    CREDITS_FILE,
    DEFAULT_CPU_PCT,
    DEFAULT_MINUTES_PER_HOUR,
    DEFAULT_UNITS_PER_HOUR,
    accrue,
    allowance_reached,
    credits_payload,
    lending_config,
    read_credits,
)
from .resources import discover
from .state import (
    log_path,
    read_runtime,
    read_worker_status,
    running_pid,
    start_daemon,
    stop_daemon,
    tail_log,
    write_worker_status,
)
from .worker import run_worker

DASHBOARD_HINT = "Monitor: {server}/"


def _api(config):
    return ApiClient(
        config["server_url"],
        token=config.get("token"),
        timeout=config.get("request_timeout", 20),
    )


def _age(iso):
    if not iso:
        return "never"
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _mb(value):
    if value is None:
        return "unavailable"
    value = float(value)
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{round(value)} MB"


def _pct(value):
    if value is None:
        return "unavailable"
    return f"{value}%"


def _yes_no(value):
    if value is None:
        return "unavailable"
    return "yes" if value else "no"


def _register(config):
    if not config.get("enrollment_key"):
        raise RuntimeError(
            "enrollment key not set; run: primal init --enrollment-key <key> "
            "or set PRIMAL_ENROLLMENT_KEY"
        )
    info = device_info(config)
    client = _api(config)
    response = client.register(config["enrollment_key"], config["install_id"], info)
    config["device_id"] = response["device_id"]
    config["token"] = response["token"]
    save_config(config)
    return response


def _ensure_registered(config, announce=True):
    if config.get("device_id") and config.get("token"):
        return config
    response = _register(config)
    if announce:
        print(f"Registered device {response['device_id']}")
        print(f"Token stored in {CONFIG_PATH} (mode 600)")
    return load_config()


def _ensure_heartbeat(config):
    pid = running_pid("heartbeat")
    if pid:
        return pid
    pid = start_daemon("heartbeat", ["_heartbeat"])
    print(f"Heartbeat daemon started (pid {pid}, log {log_path('heartbeat')})")
    return pid


def _heartbeat_now(config, quiet=False):
    try:
        response, metrics, worker = send_once(config)
        if not quiet:
            print(
                "Heartbeat OK: "
                f"status={response.get('status')} "
                f"cpu={_pct(metrics.get('cpu_usage_pct'))} "
                f"ram_available={_mb(metrics.get('ram_available_mb'))} "
                f"worker={worker.get('status')}"
            )
        return True
    except (ApiError, RuntimeError, OSError) as error:
        if not quiet:
            print(f"Heartbeat failed: {error}", file=sys.stderr)
        return False


def cmd_init(args):
    config = init_config(
        server=args.server,
        enrollment_key=args.enrollment_key or os.environ.get("PRIMAL_ENROLLMENT_KEY"),
        name=args.name,
        force=args.force,
    )
    info = device_info(config)
    print(f"Config: {CONFIG_PATH}")
    print(f"Server: {config['server_url']}")
    print(f"Device: {info['name']} ({info['manufacturer']} {info['model']})")
    print(f"Enrollment key: {masked(config.get('enrollment_key'))}")
    if config.get("device_id"):
        print(f"Device ID: {config['device_id']} (existing registration kept)")
    else:
        print("Device ID: not registered yet")
    print("Next: primal discover")
    return 0


def _print_discovery(data):
    info = data
    metrics = data.get("metrics", {})
    print(f"Primal Space CLI {info.get('cli_version')}")
    print(f"Device: {info.get('manufacturer')} {info.get('model')}")
    print(f"Name: {info.get('name')}")
    print(f"Android: {info.get('android_version')} (SDK {info.get('android_sdk') or 'unavailable'})")
    cpu_usage = metrics.get("cpu_usage_pct")
    cpu_text = (
        f"{_pct(cpu_usage)} used"
        if cpu_usage is not None
        else f"utilization unavailable, load {_pct(metrics.get('cpu_load_pct'))} of {info.get('cpu_cores') or '?'} cores"
    )
    print(f"CPU: {info.get('cpu_arch') or 'unavailable'}, {info.get('cpu_cores') or 'unavailable'} cores, {cpu_text}")
    print(f"RAM: {_mb(metrics.get('ram_total_mb'))} total, {_mb(metrics.get('ram_available_mb'))} available, {_pct(metrics.get('ram_used_pct'))} used")
    print(f"Storage free: {_mb(metrics.get('storage_free_mb'))} (Termux app-private view)")
    print(f"Battery: {_pct(metrics.get('battery_pct'))}, charging={_yes_no(metrics.get('charging'))}")
    print(f"Network: {metrics.get('network_type') or 'unknown'}, connected={_yes_no(metrics.get('network_connected'))}")
    print(f"Worker: {data.get('worker_status') or 'stopped'}")
    print(f"Last heartbeat: {_age(data.get('last_heartbeat'))}")


def cmd_discover(args):
    config = load_config()
    data = discover(config)
    worker = read_worker_status()
    if running_pid("worker"):
        worker_status = worker.get("status") or "idle"
    elif config.get("compute_enabled"):
        worker_status = "stopped"
    else:
        worker_status = "disabled"
    data["worker_status"] = worker_status
    data["last_heartbeat"] = read_runtime().get("last_heartbeat_ok")
    data["compute_enabled"] = bool(config.get("compute_enabled"))

    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        _print_discovery(data)
        print(f"Compute enabled: {_yes_no(data['compute_enabled'])}")
    return 0


def _apply_compute_mode(config):
    mode = config.get("compute_mode") or "off"
    if mode == "lend":
        if config.get("auto_lend_on_start", True):
            config["lending_enabled"] = True
            config["compute_enabled"] = True
    elif mode == "ondemand":
        config["lending_enabled"] = False
    else:
        config["lending_enabled"] = False
        config["compute_enabled"] = False
    save_config(config)
    return config


def cmd_connect(args):
    config = load_config()
    config = _ensure_registered(config)
    config = _apply_compute_mode(config)
    _heartbeat_now(config)
    if args.foreground:
        print("Running heartbeat in the foreground. Ctrl-C to stop.")
        run_heartbeat()
        return 0
    pid = _ensure_heartbeat(config)
    print(f"Heartbeat running (pid {pid})")
    if (config.get("compute_mode") or "off") == "lend" and config.get("auto_lend_on_start", True):
        print("Active compute: lending is enabled for this session.")
    print(DASHBOARD_HINT.format(server=config["server_url"]))
    return 0


def _server_status(config):
    if not config.get("device_id"):
        return None, "not registered"
    try:
        return _api(config).get_device(config["device_id"]), None
    except ApiError as error:
        return None, str(error)


def cmd_status(args):
    config = load_config()
    runtime = read_runtime()
    worker = read_worker_status()
    payload = {
        "config_path": str(CONFIG_PATH),
        "server_url": config.get("server_url"),
        "device_id": config.get("device_id"),
        "token": masked(config.get("token")),
        "enrollment_key": masked(config.get("enrollment_key")),
        "compute_enabled": bool(config.get("compute_enabled")),
        "worker_paused": bool(config.get("worker_paused")),
        "heartbeat_pid": running_pid("heartbeat"),
        "worker_pid": running_pid("worker"),
        "worker_status": worker.get("status") or "stopped",
        "worker_current_task": worker.get("current_task_id"),
        "last_heartbeat_ok": runtime.get("last_heartbeat_ok"),
        "last_heartbeat_error": runtime.get("last_error"),
        "server": None,
        "server_error": None,
    }
    server, error = _server_status(config)
    payload["server"] = server
    payload["server_error"] = error

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Server: {payload['server_url']}")
    print(f"Device ID: {payload['device_id'] or 'not registered'}")
    print(f"Token: {payload['token']}")
    print(f"Compute enabled: {_yes_no(payload['compute_enabled'])}")
    print(f"Heartbeat daemon: {'running pid ' + str(payload['heartbeat_pid']) if payload['heartbeat_pid'] else 'stopped'}")
    print(f"Worker daemon: {'running pid ' + str(payload['worker_pid']) if payload['worker_pid'] else 'stopped'}")
    print(f"Worker status: {payload['worker_status']}" + (f" (task {payload['worker_current_task']})" if payload['worker_current_task'] else ""))
    print(f"Last heartbeat: {_age(payload['last_heartbeat_ok'])}")
    if payload["last_heartbeat_error"]:
        print(f"Heartbeat error: {payload['last_heartbeat_error']}")

    if server:
        device = server.get("device", {})
        metrics = device.get("metrics", {})
        print("")
        print(f"Monitor status: {device.get('status')}")
        print(f"Monitor last heartbeat: {_age(device.get('last_heartbeat'))}")
        print(f"Monitor worker: {device.get('worker', {}).get('status')} · compute={_yes_no(device.get('worker', {}).get('compute_enabled'))}")
        cpu_usage = metrics.get("cpu_usage_pct")
        cpu_text = _pct(cpu_usage) if cpu_usage is not None else f"load {_pct(metrics.get('cpu_load_pct'))}"
        print(f"CPU: {cpu_text} · RAM available: {_mb(metrics.get('ram_available_mb'))} · Battery: {_pct(metrics.get('battery_pct'))}")
        tasks = server.get("tasks") or []
        if tasks:
            task = tasks[0]
            print(f"Latest task: {task.get('id', '')[:8]} {task.get('status')} verified={task.get('verified')} value={task.get('result_value')}")
    elif error:
        print(f"Monitor query failed: {error}")
    return 0


def _ram_bar(used_pct):
    if used_pct is None:
        return "[unavailable]"
    filled = int(max(0, min(100, used_pct)) / 5)
    return "[" + "#" * filled + "." * (20 - filled) + f"] {used_pct}%"


def cmd_monitor(args):
    config = load_config()
    if not config.get("device_id"):
        raise RuntimeError("device is not registered; run: primal connect")
    interval = max(2, int(args.interval))
    while True:
        server, error = _server_status(config)
        if args.once:
            pass
        else:
            print("\033[2J\033[H", end="")
        print(f"PRIMAL SPACE · {config['server_url']} · {time.strftime('%H:%M:%S')}")
        if error:
            print(f"Monitor unavailable: {error}")
        else:
            device = server.get("device", {})
            metrics = device.get("metrics", {})
            worker = device.get("worker", {})
            print(f"Device: {device.get('name')} ({device.get('manufacturer')} {device.get('model')})")
            print(f"Status: {device.get('status')} · last heartbeat {_age(device.get('last_heartbeat'))}")
            cpu_usage = metrics.get("cpu_usage_pct")
            cpu_text = (
                f"{_pct(cpu_usage)} used"
                if cpu_usage is not None
                else f"util unavailable, load {_pct(metrics.get('cpu_load_pct'))}"
            )
            print(f"Android: {device.get('android_version')} · CPU: {device.get('cpu_arch')} {device.get('cpu_cores')} cores @ {cpu_text}")
            print(f"RAM: {_mb(metrics.get('ram_available_mb'))} free of {_mb(metrics.get('ram_total_mb'))} {_ram_bar(metrics.get('ram_used_pct'))}")
            print(f"Storage free: {_mb(metrics.get('storage_free_mb'))} · Battery: {_pct(metrics.get('battery_pct'))} · charging={_yes_no(metrics.get('charging'))}")
            print(f"Network: {metrics.get('network_type')} · connected={_yes_no(metrics.get('network_connected'))}")
            print(f"Worker: {worker.get('status')} · compute={_yes_no(worker.get('compute_enabled'))} · paused={_yes_no(worker.get('paused'))}")
            if worker.get("current_task_id"):
                print(f"Current task: {worker.get('current_task_id')}")
            tasks = server.get("tasks") or []
            active = [task for task in tasks if task.get("status") in {"pending", "claimed", "executing"}]
            print(f"Active tasks: {len(active)}")
            for task in active[:3]:
                print(f"  - {task.get('id', '')[:8]} {task.get('status')} {task.get('workload')}")
        if args.once:
            break
        time.sleep(interval)
    return 0


def cmd_worker(args):
    action = args.worker_command
    config = load_config()

    if action == "start":
        mode = config.get("compute_mode") or "off"
        if mode == "off":
            raise RuntimeError("compute mode is off; set it with: primal settings mode ondemand|lend")
        if not config.get("compute_enabled"):
            raise RuntimeError("compute is disabled; enable explicitly with: primal compute enable")
        if not config.get("device_id") or not config.get("token"):
            raise RuntimeError("device is not registered; run: primal connect")
        _ensure_heartbeat(config)
        config["worker_paused"] = False
        config["cancel_current"] = False
        save_config(config)
        pid = start_daemon("worker", ["_worker"])
        print(f"Worker started (pid {pid}, log {log_path('worker')})")
        print(
            "Limits: "
            f"cpu<={config.get('maximum_cpu_usage')}% "
            f"ram<={config.get('maximum_memory_usage_mb')}MB "
            f"battery>={config.get('minimum_battery')}%"
        )
        return 0

    if action == "stop":
        pid = stop_daemon("worker", timeout=25)
        config["cancel_current"] = False
        save_config(config)
        if pid:
            print(f"Worker stopped (pid {pid}). Any executing task was cancelled safely.")
        else:
            print("Worker is not running.")
        write_worker_status({"status": "stopped", "current_task_id": None})
        _heartbeat_now(config, quiet=True)
        return 0

    if action in {"pause", "resume"}:
        config["worker_paused"] = action == "pause"
        save_config(config)
        print(f"Worker {'paused (no new tasks)' if action == 'pause' else 'resumed'}.")
        _heartbeat_now(config, quiet=True)
        return 0

    if action == "cancel":
        status = read_worker_status()
        if status.get("status") != "running" or not status.get("current_task_id"):
            print("No task is currently executing.")
            return 0
        config["cancel_current"] = True
        save_config(config)
        print(f"Cancellation requested for {status.get('current_task_id')}; worker will abort safely.")
        return 0

    raise RuntimeError(f"unknown worker command: {action}")


def cmd_compute(args):
    config = load_config()
    enabled = args.compute_command == "enable"
    config["compute_enabled"] = enabled
    config["cancel_current"] = not enabled
    save_config(config)
    if enabled:
        print("Compute explicitly ENABLED. Start the worker with: primal worker start")
    else:
        print("Compute DISABLED. No new work will be claimed and any current task will abort.")
    _heartbeat_now(config, quiet=True)
    return 0


def cmd_config(args):
    if args.config_command == "show":
        config = load_config()
        shown = {}
        for key in sorted(DEFAULTS):
            value = config.get(key)
            if key in {"token", "enrollment_key"}:
                value = masked(value)
            shown[key] = value
        if args.json:
            print(json.dumps(shown, indent=2, sort_keys=True))
        else:
            for key, value in shown.items():
                print(f"{key}: {value}")
        return 0

    key = args.key
    try:
        config = set_config_value(key, args.value)
    except KeyError:
        print(f"Unknown config key: {key}", file=sys.stderr)
        return 2
    if key in {"token", "enrollment_key"}:
        print(f"{key}: {masked(config.get(key))}")
    else:
        print(f"{key}: {config.get(key)}")
    if key in {
        "compute_enabled",
        "worker_paused",
        "cancel_current",
        "maximum_cpu_usage",
        "maximum_memory_usage_mb",
        "minimum_battery",
    }:
        _heartbeat_now(config, quiet=True)
    return 0


def cmd_disconnect(args):
    config = load_config()
    worker_pid = stop_daemon("worker", timeout=25)
    heartbeat_pid = stop_daemon("heartbeat", timeout=8)
    write_worker_status({"status": "stopped", "current_task_id": None})
    server_result = None
    server_error = None
    if config.get("device_id") and config.get("token"):
        try:
            server_result = _api(config).disconnect(config["device_id"])
        except ApiError as error:
            server_error = str(error)

    if worker_pid:
        print(f"Worker stopped (pid {worker_pid}).")
    if heartbeat_pid:
        print(f"Heartbeat stopped (pid {heartbeat_pid}).")
    if server_result:
        print(f"Monitor marked device offline at {server_result.get('disconnected_at')}.")
    elif server_error:
        print(f"Local processes stopped, but monitor disconnect failed: {server_error}", file=sys.stderr)
    print("Reconnect with: primal connect")
    return 0


def cmd_message_send(args):
    config = load_config()
    if not config.get("device_id") or not config.get("token"):
        print("Device is not registered; run: primal connect")
        return 1
    target = None if args.target.lower() in {"all", "*", "broadcast"} else args.target
    client = _api(config)
    if target:
        try:
            peers = client.get_peers(config["device_id"]).get("peers") or []
        except ApiError:
            peers = []
        matches = [peer for peer in peers if str(peer.get("name", "")).lower() == target.lower()]
        if matches:
            target = matches[0]["id"]
        elif not any(peer.get("id") == target for peer in peers):
            print(f"Unknown device: {args.target}. Use 'primal status' device ids or 'all'.")
            return 1
    try:
        client.send_message(config["device_id"], target, args.text)
    except ApiError as error:
        print(f"Could not send message: {error}")
        return 1
    print("Message sent to all devices." if target is None else "Message sent.")
    return 0


def cmd_message_inbox(args):
    config = load_config()
    if not config.get("device_id") or not config.get("token"):
        print("Device is not registered; run: primal connect")
        return 1
    try:
        payload = _api(config).get_messages(config["device_id"])
    except ApiError as error:
        print(f"Could not read messages: {error}")
        return 1
    messages = payload.get("messages") or []
    if not messages:
        print("Inbox is empty.")
        return 0
    for message in messages[-max(1, args.limit):]:
        target = message.get("to") or "all"
        print(f"{message.get('created_at')}  {message.get('from')} -> {target}: {message.get('body')}")
    return 0


def cmd_lend(args):
    config = load_config()
    settings = lending_config(config)
    if args.lend_command == "status":
        credits = read_credits()
        used = allowance_reached(config, credits)
        print(f"Active compute: {'ON' if settings['enabled'] else 'OFF'}")
        print(f"CPU offered: {settings['cpu_pct']}%")
        print(f"Allowance: {settings['minutes_per_hour']} min/hour")
        print(f"Rate: {settings['units_per_hour']} unit(s) per hour of compute lent")
        print(f"Units banked: {credits.get('units', 0.0):.4f} (earned {credits.get('total_units', 0.0):.4f})")
        print(f"Used this hour: {credits.get('minutes_used', 0.0):.1f} min" + (" · limit reached" if used else ""))
        return 0

    if args.lend_command == "enable":
        cpu = args.cpu if args.cpu is not None else settings["cpu_pct"]
        minutes = args.minutes if args.minutes is not None else settings["minutes_per_hour"]
        units = args.units_per_hour if args.units_per_hour is not None else settings["units_per_hour"]
        config["lending_enabled"] = True
        config["lend_cpu_pct"] = max(1, min(100, int(cpu)))
        config["lend_minutes_per_hour"] = max(1, min(60, int(minutes)))
        config["lend_units_per_hour"] = max(0.01, min(100.0, float(units)))
        save_config(config)
        print(f"Active compute ON: {config['lend_cpu_pct']}% CPU, {config['lend_minutes_per_hour']} min/hour, {config['lend_units_per_hour']} unit(s)/hour")
        print("Compute units are stored on this device.")
        return 0

    config["lending_enabled"] = False
    save_config(config)
    print("Active compute OFF. Banked units are kept.")
    return 0


def cmd_credits(args):
    credits = read_credits()
    history = credits.get("history") or {}
    wallet = read_wallet()
    print(f"Wallet balance: {wallet.get('balance', 0.0):.4f} CU (sent {wallet.get('spent_total', 0.0):.4f})")
    print(f"Compute units banked: {credits.get('units', 0.0):.4f}")
    print(f"Compute units earned: {credits.get('total_units', 0.0):.4f}")
    if history:
        print("Daily:")
        for day in sorted(history)[-7:]:
            print(f"  {day}: {history[day]:.4f}")
    print(f"Stored at: {CREDITS_FILE}")
    return 0


def cmd_wallet(args):
    config = load_config()
    wallet = roll_periods(read_wallet())
    payload = wallet_payload(wallet)
    usage = caps_payload(config, wallet)
    limits = compute_caps(config)
    print(f"Balance: {payload['balance']:.4f} CU")
    print(f"Earned: {payload['earned_total']:.4f} CU · sent: {payload['spent_total']:.4f} CU")
    if limits["day_minutes"] or limits["week_minutes"] or limits["month_minutes"]:
        print(
            f"Compute caps: day {usage['day_used']:.0f}/{limits['day_minutes'] or '∞'}m · "
            f"week {usage['week_used']:.0f}/{limits['week_minutes'] or '∞'}m · "
            f"month {usage['month_used']:.0f}/{limits['month_minutes'] or '∞'}m"
        )
    else:
        print("Compute caps: unlimited")
    history = wallet.get("history") or []
    if history and args.history:
        print("Recent activity:")
        for entry in history[-args.history:]:
            print(f"  {entry.get('at')} {entry.get('kind')} {entry.get('amount')} CU → balance {entry.get('balance')}")
    print(f"Stored at: {WALLET_FILE}")
    return 0


def cmd_profile(args):
    config = load_config()
    if args.profile_command == "show":
        print(f"Name: {config.get('profile_name') or '(device name) ' + str(config.get('device_name'))}")
        print(f"Status: {config.get('profile_status') or '-'}")
        print(f"Avatar: {config.get('profile_avatar') or '-'}")
        return 0
    if args.name is not None:
        config["profile_name"] = args.name[:48]
    if args.status is not None:
        config["profile_status"] = args.status[:120]
    if args.avatar is not None:
        config["profile_avatar"] = args.avatar[:8]
    save_config(config)
    print("Profile updated.")
    return 0


def cmd_settings(args):
    config = load_config()
    mode = config.get("compute_mode") or "off"
    if args.settings_command == "show":
        usage = caps_payload(config)
        limits = compute_caps(config)
        print(f"Compute mode: {mode}")
        print(f"Auto-start lending on connect: {'yes' if config.get('auto_lend_on_start') else 'no'}")
        print(f"Lending: {settings_line(config)}")
        print(
            f"Caps: day {limits['day_minutes'] or '∞'}m · week {limits['week_minutes'] or '∞'}m · month {limits['month_minutes'] or '∞'}m"
        )
        print(f"Used: day {usage['day_used']:.0f}m · week {usage['week_used']:.0f}m · month {usage['month_used']:.0f}m")
        blocked = cap_reached(config, roll_periods(read_wallet()))
        print(f"Status: {blocked or 'within limits'}")
        return 0

    if args.settings_command == "mode":
        config["compute_mode"] = args.mode
        if args.mode == "lend":
            config["lending_enabled"] = True
            config["compute_enabled"] = True
        elif args.mode == "off":
            config["lending_enabled"] = False
            config["compute_enabled"] = False
        save_config(config)
        print(f"Compute mode set to {args.mode}.")
        return 0

    if args.settings_command == "auto":
        config["auto_lend_on_start"] = args.enabled
        save_config(config)
        print(f"Auto-start lending on connect: {'yes' if args.enabled else 'no'}")
        return 0

    if args.settings_command == "caps":
        if args.day is not None:
            config["max_compute_minutes_day"] = max(0, int(args.day))
        if args.week is not None:
            config["max_compute_minutes_week"] = max(0, int(args.week))
        if args.month is not None:
            config["max_compute_minutes_month"] = max(0, int(args.month))
        save_config(config)
        print("Compute caps updated.")
        return 0
    return 0


def settings_line(config):
    from .lending import lending_config

    lending = lending_config(config)
    return (
        f"{'on' if lending['enabled'] else 'off'} · {lending['cpu_pct']}% CPU · "
        f"{lending['minutes_per_hour']} min/hour · {lending['units_per_hour']} unit(s)/hour"
    )


def cmd_message_threads(args):
    config = load_config()
    if not config.get("device_id") or not config.get("token"):
        print("Device is not registered; run: primal connect")
        return 1
    try:
        payload = _api(config).get_messages(config["device_id"])
    except ApiError as error:
        print(f"Could not read messages: {error}")
        return 1
    messages = payload.get("messages") or []
    if not messages:
        print("No conversations yet.")
        return 0
    threads = {}
    for message in messages:
        peer = message.get("from") if message.get("from") != config.get("device_id") else message.get("to")
        peer = peer or "broadcast"
        threads.setdefault(peer, []).append(message)
    try:
        peers = {peer.get("id"): peer for peer in (_api(config).get_peers(config["device_id"]).get("peers") or [])}
    except ApiError:
        peers = {}
    for peer, items in sorted(threads.items()):
        label = peers.get(peer, {}).get("name") or ("all devices" if peer == "broadcast" else peer[:8])
        last = items[-1]
        print(f"{label}: {len(items)} message(s) · last: {last.get('body')[:80]}")
    return 0


def cmd_logs(args):
    name = args.name
    lines = tail_log(name, args.lines)
    if not lines:
        print(f"No log at {log_path(name)}")
        return 0
    print("\n".join(lines))
    return 0


def cmd_desktop(_args):
    try:
        from .desktop import main as desktop_main
    except ImportError as error:
        raise RuntimeError(
            "the desktop window needs tkinter; install python3-tk or use the packaged binary"
        ) from error
    return desktop_main()


def cmd_sandbox_child(args):
    from .sandbox import child_main

    return child_main(args.payload)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="primal",
        description="Primal Space compute node for Android/Termux, Windows, macOS and Linux",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create or update local config")
    init_parser.add_argument("--server", help="monitor base URL")
    init_parser.add_argument("--enrollment-key", help="enrollment key issued by the operator")
    init_parser.add_argument("--name", help="device display name")
    init_parser.add_argument("--force", action="store_true", help="reset identity and registration")
    init_parser.set_defaults(func=cmd_init)

    discover_parser = subparsers.add_parser("discover", help="collect local device resources")
    discover_parser.add_argument("--json", action="store_true")
    discover_parser.set_defaults(func=cmd_discover)

    connect_parser = subparsers.add_parser("connect", help="register and start heartbeats")
    connect_parser.add_argument("--foreground", action="store_true", help="run heartbeat in foreground")
    connect_parser.set_defaults(func=cmd_connect)

    status_parser = subparsers.add_parser("status", help="show local and monitor status")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    monitor_parser = subparsers.add_parser("monitor", help="live terminal monitor")
    monitor_parser.add_argument("--interval", type=int, default=5)
    monitor_parser.add_argument("--once", action="store_true")
    monitor_parser.set_defaults(func=cmd_monitor)

    worker_parser = subparsers.add_parser("worker", help="control the compute worker")
    worker_sub = worker_parser.add_subparsers(dest="worker_command", required=True)
    for action in ("start", "stop", "pause", "resume", "cancel"):
        worker_sub.add_parser(action)
    worker_parser.set_defaults(func=cmd_worker)

    compute_parser = subparsers.add_parser("compute", help="explicitly enable or disable compute")
    compute_sub = compute_parser.add_subparsers(dest="compute_command", required=True)
    compute_sub.add_parser("enable")
    compute_sub.add_parser("disable")
    compute_parser.set_defaults(func=cmd_compute)

    config_parser = subparsers.add_parser("config", help="show or change contribution settings")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show")
    config_show.add_argument("--json", action="store_true")
    config_set = config_sub.add_parser("set")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_parser.set_defaults(func=cmd_config)

    disconnect_parser = subparsers.add_parser("disconnect", help="stop daemons and mark offline")
    disconnect_parser.set_defaults(func=cmd_disconnect)

    wallet_parser = subparsers.add_parser("wallet", help="show compute currency balance and caps")
    wallet_parser.add_argument("--history", type=int, default=0, help="show recent wallet activity")
    wallet_parser.set_defaults(func=cmd_wallet)

    profile_parser = subparsers.add_parser("profile", help="set your profile shown to other devices")
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_show = profile_sub.add_parser("show")
    profile_show.set_defaults(func=cmd_profile)
    profile_set = profile_sub.add_parser("set")
    profile_set.add_argument("--name")
    profile_set.add_argument("--status")
    profile_set.add_argument("--avatar")
    profile_set.set_defaults(func=cmd_profile)

    settings_parser = subparsers.add_parser("settings", help="compute mode, caps and behaviour")
    settings_sub = settings_parser.add_subparsers(dest="settings_command", required=True)
    settings_show = settings_sub.add_parser("show")
    settings_show.set_defaults(func=cmd_settings)
    settings_mode = settings_sub.add_parser("mode", help="off | ondemand | lend")
    settings_mode.add_argument("mode", choices=["off", "ondemand", "lend"])
    settings_mode.set_defaults(func=cmd_settings)
    settings_auto = settings_sub.add_parser("auto", help="start lending automatically when the daemon starts")
    settings_auto.add_argument("enabled", choices=["on", "off"])
    settings_auto.set_defaults(func=cmd_settings)
    settings_caps = settings_sub.add_parser("caps", help="maximum compute per day, week and month (minutes)")
    settings_caps.add_argument("--day", type=int)
    settings_caps.add_argument("--week", type=int)
    settings_caps.add_argument("--month", type=int)
    settings_caps.set_defaults(func=cmd_settings)

    lend_parser = subparsers.add_parser("lend", help="active compute: lend compute and earn units")
    lend_sub = lend_parser.add_subparsers(dest="lend_command", required=True)
    lend_status = lend_sub.add_parser("status", help="show lending settings and units")
    lend_status.set_defaults(func=cmd_lend)
    lend_enable = lend_sub.add_parser("enable", help="start lending compute")
    lend_enable.add_argument("--cpu", type=int, help=f"CPU share to offer (1-100, default {DEFAULT_CPU_PCT})")
    lend_enable.add_argument("--minutes", type=int, help=f"minutes per hour to lend (1-60, default {DEFAULT_MINUTES_PER_HOUR})")
    lend_enable.add_argument("--units-per-hour", type=float, help=f"units earned per hour of compute (default {DEFAULT_UNITS_PER_HOUR})")
    lend_enable.set_defaults(func=cmd_lend)
    lend_disable = lend_sub.add_parser("disable", help="stop lending compute")
    lend_disable.set_defaults(func=cmd_lend)
    credits_parser = subparsers.add_parser("credits", help="show compute units banked on this device")
    credits_parser.set_defaults(func=cmd_credits)

    message_parser = subparsers.add_parser("message", help="send or read device messages")
    message_sub = message_parser.add_subparsers(dest="message_command", required=True)
    message_send = message_sub.add_parser("send", help="send a message to a device or all devices")
    message_send.add_argument("target", help="device id, device name, or 'all'")
    message_send.add_argument("text", help="message body")
    message_send.set_defaults(func=cmd_message_send)
    message_inbox = message_sub.add_parser("inbox", help="read recent messages")
    message_inbox.add_argument("--limit", type=int, default=20)
    message_inbox.set_defaults(func=cmd_message_inbox)
    message_threads = message_sub.add_parser("threads", help="list conversations")
    message_threads.set_defaults(func=cmd_message_threads)

    logs_parser = subparsers.add_parser("logs", help="show daemon logs")
    logs_parser.add_argument("name", choices=["heartbeat", "worker"])
    logs_parser.add_argument("--lines", type=int, default=30)
    logs_parser.set_defaults(func=cmd_logs)

    desktop_parser = subparsers.add_parser(
        "desktop", help="open the PrimalSpace desktop window (Windows, macOS, Linux)"
    )
    desktop_parser.set_defaults(func=cmd_desktop)

    subparsers.add_parser("_heartbeat", help=argparse.SUPPRESS).set_defaults(func=lambda _args: run_heartbeat())
    subparsers.add_parser("_worker", help=argparse.SUPPRESS).set_defaults(func=lambda _args: run_worker())
    sandbox_parser = subparsers.add_parser("_sandbox", help=argparse.SUPPRESS)
    sandbox_parser.add_argument("payload")
    sandbox_parser.set_defaults(func=cmd_sandbox_child)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except ApiError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
