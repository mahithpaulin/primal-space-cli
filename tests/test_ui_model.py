from primal.ui_model import (
    COMMUNITY_LABEL,
    compute_plan,
    compute_summary,
    conversation_targets,
    group_member_summary,
    message_lines,
)


def test_compute_plan_defaults_and_clamps():
    plan = compute_plan({})
    assert plan["mode"] == "off"
    assert plan["cpu_pct"] == 20
    assert plan["minutes_per_hour"] == 30
    assert plan["units_per_hour"] == 1.0

    clamped = compute_plan(
        {
            "compute_mode": "lend",
            "lend_cpu_pct": 500,
            "lend_minutes_per_hour": 0,
            "lend_units_per_hour": 0,
            "max_compute_minutes_day": -5,
        }
    )
    assert clamped["mode"] == "lend"
    assert clamped["cpu_pct"] == 100
    assert clamped["minutes_per_hour"] == 1
    assert clamped["units_per_hour"] == 0.01
    assert clamped["cap_day"] == 0


def test_compute_summary_uses_settled_numbers():
    summary = compute_summary(
        {"compute_mode": "lend", "lend_minutes_per_hour": 30},
        {"balance": 1.2345, "earned_total": 2.5, "spent_total": 0.25},
        {"minutes_used": 12.5, "total_units": 2.75, "updated_at": None},
        {"day_used": 3.0, "week_used": 10.0, "month_used": 20.0},
    )
    assert summary["mode_label"] == "Lend"
    assert summary["balance"] == "1.2345 CU"
    assert summary["earned"] == "2.5000 CU"
    assert summary["spent"] == "0.2500 CU"
    assert summary["hour_text"] == "12.5 / 30 min"
    assert summary["hour_pct"] == 41
    assert summary["lifetime"] == "2.7500 CU"
    assert summary["last_settled"] == "never"
    assert "day 3/∞ min" in summary["caps_text"]


def test_conversation_targets_lists_community_peers_and_groups():
    peers = [
        {"id": "peer-1", "name": "Pixel", "status": "online", "profile": {"name": "Pixel One"}},
        {"id": "me", "name": "Mine"},
    ]
    groups = [{"id": "group-1", "name": "Crew", "members": []}]
    targets = conversation_targets(peers, groups, "me")
    labels = [label for label, _ in targets]
    assert targets[0] == (COMMUNITY_LABEL, None)
    assert "Pixel One (online)" in labels
    assert "Group · Crew" in labels
    assert dict(targets)["Group · Crew"] == "group:group-1"
    assert "Mine (offline)" not in labels


def test_message_lines_resolve_names_and_mark_own_messages():
    peers = [{"id": "peer-1", "name": "Pixel", "profile": {"name": "Pixel One"}}]
    groups = [{"id": "group-1", "name": "Crew", "members": [{"id": "peer-1", "name": "Pixel One"}]}]
    messages = [
        {
            "id": "m1",
            "from": "peer-1",
            "to": "me",
            "topic": "chat",
            "body": "hello",
            "created_at": "2026-01-01T00:00:00.000Z",
        },
        {
            "id": "m2",
            "from": "me",
            "to": "peer-1",
            "topic": "chat",
            "body": "hi back",
            "created_at": "2026-01-01T00:01:00.000Z",
        },
    ]
    lines = message_lines(messages, peers, groups, "me")
    assert any("Pixel One -> you" in line for line in lines)
    assert any("(you)" in line for line in lines)
    assert "    hello" in lines


def test_group_member_summary_counts_members():
    group = {
        "members": [
            {"id": "a", "name": "Ada", "online": True},
            {"id": "b", "name": "Bob"},
        ]
    }
    summary = group_member_summary(group)
    assert "2 member(s)" in summary
    assert "1 online" in summary
    assert "Ada" in summary and "Bob" in summary
