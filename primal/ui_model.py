"""Pure view-model helpers shared by the desktop app and its tests."""

from datetime import datetime, timezone

MODES = ("off", "ondemand", "lend")
MODE_LABELS = {"off": "Off", "ondemand": "On demand", "lend": "Lend"}
COMMUNITY_LABEL = "Community (all devices)"


def format_age(value):
    if not value:
        return "never"
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    except (ValueError, TypeError):
        return str(value)
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _integer(value, default=0):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_plan(config):
    config = config or {}
    return {
        "mode": (config.get("compute_mode") or "off").strip().lower(),
        "cpu_pct": max(1, min(100, _integer(config.get("lend_cpu_pct"), 20))),
        "minutes_per_hour": max(1, min(60, _integer(config.get("lend_minutes_per_hour"), 30))),
        "units_per_hour": max(0.01, min(100.0, _number(config.get("lend_units_per_hour"), 1.0))),
        "auto_lend": bool(config.get("auto_lend_on_start", True)),
        "cap_day": max(0, _integer(config.get("max_compute_minutes_day"), 0)),
        "cap_week": max(0, _integer(config.get("max_compute_minutes_week"), 0)),
        "cap_month": max(0, _integer(config.get("max_compute_minutes_month"), 0)),
    }


def compute_summary(config, wallet, credits, usage=None):
    plan = compute_plan(config)
    wallet = wallet or {}
    credits = credits or {}
    usage = usage or {}
    hour_used = round(_number(credits.get("minutes_used")), 2)
    allowance = plan["minutes_per_hour"]
    return {
        "mode": plan["mode"],
        "mode_label": MODE_LABELS.get(plan["mode"], "Off"),
        "balance": f"{_number(wallet.get('balance')):.4f} CU",
        "earned": f"{_number(wallet.get('earned_total')):.4f} CU",
        "spent": f"{_number(wallet.get('spent_total')):.4f} CU",
        "hour_used": hour_used,
        "hour_allowance": allowance,
        "hour_text": f"{hour_used:.1f} / {allowance} min",
        "hour_pct": min(100, int(hour_used / allowance * 100)) if allowance else 0,
        "lifetime": f"{_number(credits.get('total_units')):.4f} CU",
        "last_settled": format_age(credits.get("updated_at")),
        "caps_text": (
            f"day {usage.get('day_used', 0):.0f}/{plan['cap_day'] or '∞'} min"
            f" · week {usage.get('week_used', 0):.0f}/{plan['cap_week'] or '∞'}"
            f" · month {usage.get('month_used', 0):.0f}/{plan['cap_month'] or '∞'}"
        ),
    }


def peer_label(peer, device_id=None):
    profile = peer.get("profile") or {}
    name = profile.get("name") or peer.get("name") or str(peer.get("id") or "")[:8] or "device"
    status = peer.get("status") or "offline"
    marker = "online" if status == "online" else status
    return f"{name} ({marker})"


def group_label(group):
    name = group.get("name") or f"group {str(group.get('id') or '')[:8]}"
    return f"Group · {name}"


def conversation_targets(peers, groups, device_id=None):
    targets = [(COMMUNITY_LABEL, None)]
    for peer in peers or []:
        peer_id = peer.get("id")
        if not peer_id or peer_id == device_id:
            continue
        targets.append((peer_label(peer, device_id), peer_id))
    for group in groups or []:
        group_id = group.get("id")
        if not group_id:
            continue
        targets.append((group_label(group), f"group:{group_id}"))
    return targets


def message_lines(messages, peers, groups, device_id=None, limit=40):
    names = {}
    for peer in peers or []:
        peer_id = peer.get("id")
        if peer_id:
            names[peer_id] = (peer.get("profile") or {}).get("name") or peer.get("name") or peer_id[:8]
    for group in groups or []:
        for member in group.get("members") or []:
            member_id = member.get("id")
            if member_id and member_id not in names:
                names[member_id] = member.get("name") or member_id[:8]

    lines = []
    for message in list(messages or [])[-limit:]:
        sender_id = message.get("from") or "operator"
        sender = names.get(sender_id, "PrimalSpace" if sender_id == "operator" else sender_id[:8])
        target_id = message.get("to") or "all"
        if target_id == device_id:
            target = "you"
        else:
            target = names.get(target_id, "all")
        group_id = message.get("group_id")
        if group_id:
            target = f"group {str(group_id)[:8]}"
        kind = message.get("kind") or message.get("topic") or "chat"
        credit = message.get("credit")
        suffix = ""
        if isinstance(credit, dict) and credit.get("amount") is not None:
            suffix = f" [{credit.get('kind', 'credit')} {_number(credit.get('amount')):.4f} CU]"
        mine = " (you)" if sender_id == device_id else ""
        lines.append(f"{message.get('created_at', '')}  {sender}{mine} -> {target}  ({kind}){suffix}")
        body = message.get("body", "")
        if body:
            lines.append(f"    {body}")
    return lines


def group_member_summary(group):
    members = group.get("members") or []
    names = [member.get("name") or str(member.get("id") or "")[:8] for member in members]
    online = sum(1 for member in members if member.get("online"))
    suffix = f" · {online} online" if online else ""
    return f"{len(members)} member(s){suffix} · {', '.join(names)}"
