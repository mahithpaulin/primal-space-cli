import json
import time
from datetime import datetime, timedelta, timezone

from .state import STATE_DIR, ensure_dirs

WALLET_FILE = STATE_DIR / "wallet.json"
HISTORY_LIMIT = 60


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _empty_wallet():
    return {
        "balance": 0.0,
        "earned_total": 0.0,
        "spent_total": 0.0,
        "units": 0.0,
        "total_units": 0.0,
        "updated_at": None,
        "day_minutes": 0.0,
        "week_minutes": 0.0,
        "month_minutes": 0.0,
        "day_key": None,
        "week_key": None,
        "month_key": None,
        "history": [],
    }


def read_wallet():
    try:
        value = json.loads(WALLET_FILE.read_text())
        if isinstance(value, dict):
            wallet = _empty_wallet()
            wallet.update(value)
            return wallet
    except (OSError, json.JSONDecodeError):
        pass
    wallet = _empty_wallet()
    legacy = _legacy_credits()
    if legacy:
        wallet["units"] = float(legacy.get("units") or 0.0)
        wallet["total_units"] = float(legacy.get("total_units") or 0.0)
        wallet["balance"] = wallet["units"]
        wallet["earned_total"] = wallet["total_units"]
    return wallet


def _legacy_credits():
    from .lending import CREDITS_FILE

    try:
        value = json.loads(CREDITS_FILE.read_text())
        if isinstance(value, dict) and float(value.get("total_units") or 0.0) > 0:
            return value
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def write_wallet(wallet):
    ensure_dirs()
    wallet["history"] = wallet.get("history", [])[-HISTORY_LIMIT:]
    temp = WALLET_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(wallet, indent=2, sort_keys=True) + "\n")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(WALLET_FILE)


def period_keys(now=None):
    moment = datetime.now(timezone.utc) if now is None else now
    day = moment.strftime("%Y-%m-%d")
    year, week, _ = moment.isocalendar()
    month = moment.strftime("%Y-%m")
    return day, f"{year}-W{week:02d}", month


def roll_periods(wallet, now=None):
    day, week, month = period_keys(now)
    if wallet.get("day_key") != day:
        wallet["day_key"] = day
        wallet["day_minutes"] = 0.0
    if wallet.get("week_key") != week:
        wallet["week_key"] = week
        wallet["week_minutes"] = 0.0
    if wallet.get("month_key") != month:
        wallet["month_key"] = month
        wallet["month_minutes"] = 0.0
    return wallet


def caps(config):
    return {
        "day_minutes": int(config.get("max_compute_minutes_day") or 0),
        "week_minutes": int(config.get("max_compute_minutes_week") or 0),
        "month_minutes": int(config.get("max_compute_minutes_month") or 0),
    }


def caps_usage(wallet):
    return {
        "day_used": round(float(wallet.get("day_minutes") or 0.0), 2),
        "week_used": round(float(wallet.get("week_minutes") or 0.0), 2),
        "month_used": round(float(wallet.get("month_minutes") or 0.0), 2),
    }


def cap_reached(config, wallet, now=None):
    limits = caps(config)
    usage = caps_usage(roll_periods(wallet, now))
    if limits["day_minutes"] and usage["day_used"] >= limits["day_minutes"]:
        return "daily_compute_cap"
    if limits["week_minutes"] and usage["week_used"] >= limits["week_minutes"]:
        return "weekly_compute_cap"
    if limits["month_minutes"] and usage["month_used"] >= limits["month_minutes"]:
        return "monthly_compute_cap"
    return None


def accrue_wallet(config, duration_seconds, cpu_ratio=None, now=None):
    """Add earned currency and track time used against the configured caps."""
    wallet = roll_periods(read_wallet(), now)
    minutes = max(0.0, float(duration_seconds or 0.0)) / 60.0
    wallet["day_minutes"] = round(float(wallet.get("day_minutes") or 0.0) + minutes, 4)
    wallet["week_minutes"] = round(float(wallet.get("week_minutes") or 0.0) + minutes, 4)
    wallet["month_minutes"] = round(float(wallet.get("month_minutes") or 0.0) + minutes, 4)

    rate = float(config.get("lend_units_per_hour") or 1.0)
    earned = (float(duration_seconds or 0.0) / 3600.0) * rate
    if cpu_ratio is not None:
        promised = max(1, int(config.get("lend_cpu_pct") or 20)) / 100.0
        factor = min(1.0, max(0.0, float(cpu_ratio) / promised))
        earned *= factor
    earned = round(earned, 6)

    wallet["units"] = round(float(wallet.get("units") or 0.0) + earned, 6)
    wallet["total_units"] = round(float(wallet.get("total_units") or 0.0) + earned, 6)
    wallet["balance"] = round(float(wallet.get("balance") or 0.0) + earned, 6)
    wallet["earned_total"] = round(float(wallet.get("earned_total") or 0.0) + earned, 6)
    wallet["updated_at"] = _now_iso()
    wallet.setdefault("history", []).append(
        {"at": wallet["updated_at"], "kind": "earn", "amount": earned, "balance": wallet["balance"]}
    )
    write_wallet(wallet)
    return wallet, earned


def apply_credit(wallet, credit):
    """Apply an incoming/outgoing credit delta delivered over the message bus."""
    kind = credit.get("kind")
    amount = round(float(credit.get("amount") or 0.0), 6)
    wallet = roll_periods(wallet)
    transfer_id = credit.get("transfer_id")
    if transfer_id:
        history = wallet.setdefault("history", [])
        if any(entry.get("transfer_id") == transfer_id for entry in history):
            return wallet, 0.0
    if kind == "incoming":
        wallet["balance"] = round(float(wallet.get("balance") or 0.0) + amount, 6)
    elif kind == "outgoing":
        wallet["balance"] = round(max(0.0, float(wallet.get("balance") or 0.0) - amount), 6)
        wallet["spent_total"] = round(float(wallet.get("spent_total") or 0.0) + amount, 6)
    else:
        return wallet, 0.0
    wallet["updated_at"] = _now_iso()
    entry = {"at": wallet["updated_at"], "kind": kind, "amount": amount, "balance": wallet["balance"]}
    if transfer_id:
        entry["transfer_id"] = transfer_id
    wallet.setdefault("history", []).append(entry)
    write_wallet(wallet)
    return wallet, amount


def wallet_payload(wallet=None):
    value = wallet if isinstance(wallet, dict) else read_wallet()
    return {
        "balance": round(float(value.get("balance") or 0.0), 6),
        "earned_total": round(float(value.get("earned_total") or 0.0), 6),
        "spent_total": round(float(value.get("spent_total") or 0.0), 6),
        "units": round(float(value.get("units") or 0.0), 6),
        "total_units": round(float(value.get("total_units") or 0.0), 6),
        "updated_at": value.get("updated_at"),
    }


def caps_payload(config, wallet=None):
    value = wallet if isinstance(wallet, dict) else read_wallet()
    limits = caps(config)
    usage = caps_usage(roll_periods(value))
    return {
        "day_minutes": limits["day_minutes"] or None,
        "week_minutes": limits["week_minutes"] or None,
        "month_minutes": limits["month_minutes"] or None,
        **usage,
    }
