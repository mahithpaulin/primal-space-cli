"""Primal agent: a nameable ChatGPT-powered participant in PrimalSpace chats."""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .api import ApiClient, ApiError
from .chatgpt import API_BASE, access_token, login, read_credentials, status as chatgpt_status
from .config import load_config, save_config
from .state import STATE_DIR, write_json
from .wallet import read_wallet

AGENT_STATE_FILE = STATE_DIR / "agent_state.json"
DEFAULT_PERSONA = (
    "You are the PrimalSpace agent for a personal compute node. You help the owner and "
    "their peers with chat, node questions and compute tasks. Keep replies short and useful."
)

TOOLS = [
    {
        "type": "function",
        "name": "node_status",
        "description": "Report this node's compute status: wallet balance, earned units, caps and worker state.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "send_message",
        "description": "Send a message to another PrimalSpace device or to a group chat by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Device name or id, or group chat name"},
                "body": {"type": "string", "description": "Message body"},
            },
            "required": ["target", "body"],
        },
    },
]


class AgentError(RuntimeError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def agent_name(config=None):
    config = config or load_config()
    return (config.get("agent_name") or config.get("profile_name") or "Primal Agent").strip()[:48]


def agent_settings(config=None):
    config = config or load_config()
    return {
        "name": agent_name(config),
        "model": (config.get("agent_model") or "").strip()[:64],
        "enabled": bool(config.get("agent_enabled")),
        "persona": (config.get("agent_persona") or DEFAULT_PERSONA).strip()[:1200],
        "reply_all": bool(config.get("agent_reply_all")),
        "poll_interval": max(5, int(config.get("agent_poll_interval") or 20)),
    }


def set_agent_setting(key, value):
    allowed = {"agent_name", "agent_model", "agent_enabled", "agent_persona", "agent_reply_all", "agent_poll_interval"}
    if key not in allowed:
        raise KeyError(key)
    config = load_config()
    config[key] = value
    save_config(config)
    return config


def resolve_model(token, config=None):
    config = config or load_config()
    configured = (config.get("agent_model") or "").strip()
    if configured:
        return configured
    from .chatgpt import list_models

    models = list_models(token)
    if not models:
        raise AgentError("no models are available to this ChatGPT account")
    return models[0]["slug"]


def read_agent_state():
    try:
        value = json.loads(AGENT_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"processed": [], "last_run": None, "replies": 0}
    if not isinstance(value, dict):
        return {"processed": [], "last_run": None, "replies": 0}
    value.setdefault("processed", [])
    value.setdefault("last_run", None)
    value.setdefault("replies", 0)
    return value


def write_agent_state(state):
    state["processed"] = list(state.get("processed") or [])[-800:]
    write_json(AGENT_STATE_FILE, state)


def _stream_events(response):
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def responses_request(token, model, instructions, items, tools=None, timeout=180):
    payload = {
        "model": model,
        "instructions": instructions,
        "input": items,
        "store": False,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    request = urllib.request.Request(
        f"{API_BASE}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise AgentError(f"responses request failed ({error.code}): {detail}") from error
    except (urllib.error.URLError, OSError) as error:
        raise AgentError(f"responses request failed: {error}") from error
    text = []
    tool_calls = []
    completed = False
    with response:
        for event in _stream_events(response):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text.append(event.get("delta") or "")
            elif kind == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    tool_calls.append(
                        {
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "{}",
                            "call_id": item.get("call_id") or item.get("id") or "",
                        }
                    )
            elif kind == "response.failed":
                failure = (event.get("response") or {}).get("error") or {}
                raise AgentError(
                    f"response failed: {failure.get('code') or failure.get('message') or 'unknown_error'}"
                )
            elif kind == "response.completed":
                completed = True
    if not completed:
        raise AgentError("response stream ended without response.completed")
    return {"text": "".join(text).strip(), "tool_calls": tool_calls}


def _node_status_payload(config):
    wallet = read_wallet()
    settings = agent_settings(config)
    return {
        "device": config.get("device_name") or agent_name(config),
        "agent": settings["name"],
        "balance": round(float(wallet.get("balance") or 0.0), 6),
        "earned_total": round(float(wallet.get("earned_total") or 0.0), 6),
        "spent_total": round(float(wallet.get("spent_total") or 0.0), 6),
        "compute_mode": config.get("compute_mode") or "off",
        "max_compute_minutes_day": int(config.get("max_compute_minutes_day") or 0),
        "server": config.get("server_url"),
    }


def _resolve_target(client, config, target):
    groups = client.get_groups(config["device_id"]).get("groups") or []
    for group in groups:
        if str(group.get("name", "")).lower() == str(target).lower():
            return f"group:{group['id']}", group
    peers = client.get_peers(config["device_id"]).get("peers") or []
    for peer in peers:
        if str(peer.get("name", "")).lower() == str(target).lower():
            return peer["id"], None
        if peer.get("id") == target:
            return peer["id"], None
    return None, None


def _run_tool(client, config, call):
    name = call.get("name")
    try:
        arguments = json.loads(call.get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}
    if name == "node_status":
        return json.dumps(_node_status_payload(config))
    if name == "send_message":
        target = str(arguments.get("target") or "").strip()
        body = str(arguments.get("body") or "").strip()
        if not target or not body:
            return json.dumps({"ok": False, "error": "target and body are required"})
        resolved, group = _resolve_target(client, config, target)
        if not resolved:
            return json.dumps({"ok": False, "error": f"unknown target: {target}"})
        client.send_message(config["device_id"], resolved, body)
        return json.dumps({"ok": True, "sent_to": group.get("name") if group else target})
    return json.dumps({"ok": False, "error": f"unknown tool: {name}"})


def ask_agent(token, model, instructions, user_text, client=None, config=None, max_turns=4):
    config = config or load_config()
    items = [{"role": "user", "content": [{"type": "input_text", "text": user_text}]}]
    text = ""
    for _ in range(max_turns):
        result = responses_request(token, model, instructions, items, TOOLS)
        text = result["text"] or text
        calls = result["tool_calls"]
        if not calls:
            break
        for call in calls:
            output = _run_tool(client, config, call) if client else json.dumps({"ok": False})
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.get("call_id") or "",
                    "name": call.get("name") or "",
                    "arguments": call.get("arguments") or "{}",
                }
            )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.get("call_id") or "",
                    "output": output,
                }
            )
    return text.strip()


def _instructions(settings, config, groups):
    roster = ", ".join(
        f"{group.get('name')} ({len(group.get('member_ids') or [])} members)" for group in groups
    )
    return (
        f"{settings['persona']}\n\n"
        f"Your name is {settings['name']}. You run on the PrimalSpace node "
        f"'{config.get('device_name') or settings['name']}'.\n"
        f"Group chats you belong to: {roster or 'none'}.\n"
        "Reply in plain text. Use send_message only when another device should be notified, "
        "and node_status when the user asks about this node's compute or wallet."
    )


def _pending_messages(client, config, settings, groups):
    payload = client.get_messages(config["device_id"], since=None)
    messages = payload.get("messages") or []
    state = read_agent_state()
    processed = set(state.get("processed") or [])
    group_ids = {group["id"] for group in groups}
    pending = []
    for message in messages:
        if message.get("from") == config.get("device_id"):
            continue
        if message.get("kind") == "credit" or (message.get("credit")):
            continue
        if message.get("id") in processed:
            continue
        group_id = message.get("group_id")
        if group_id:
            if group_id not in group_ids:
                continue
            body = str(message.get("body") or "")
            mentions = settings["name"].lower() in body.lower()
            if not mentions and not settings["reply_all"]:
                processed.add(message["id"])
                continue
            pending.append((message, f"group:{group_id}"))
        elif message.get("to") == config.get("device_id"):
            pending.append((message, message.get("from")))
    state["processed"] = list(processed)
    write_agent_state(state)
    return pending, messages


def run_once(config=None, client=None, token=None):
    config = config or load_config()
    if not config.get("device_id") or not config.get("token"):
        raise AgentError("device is not registered; run: primal connect")
    settings = agent_settings(config)
    if token is None:
        credentials = read_credentials()
        if not credentials:
            raise AgentError("not signed in with ChatGPT; run: primal agent login")
        token, _ = access_token(credentials)
    client = client or ApiClient(
        config["server_url"], token=config.get("token"), timeout=config.get("request_timeout", 20)
    )
    model = resolve_model(token, config)
    groups = client.get_groups(config["device_id"]).get("groups") or []
    pending, messages = _pending_messages(client, config, settings, groups)
    replies = []
    for message, target in pending:
        group_id = message.get("group_id")
        context = []
        if group_id:
            for item in messages[-12:]:
                if item.get("group_id") == group_id:
                    context.append(f"{item.get('from', '')[:8]}: {item.get('body', '')}")
        else:
            context = [f"{item.get('from', '')[:8]}: {item.get('body', '')}" for item in messages[-6:]]
        prompt = (
            f"Group chat: {message.get('group_id') or 'direct'}\n"
            f"Recent messages:\n" + "\n".join(context[-10:]) + "\n\n"
            f"New message from {message.get('from', '')[:8]}: {message.get('body', '')}\n\n"
            "Reply as the agent."
        )
        text = ask_agent(
            token,
            model,
            _instructions(settings, config, groups),
            prompt,
            client=client,
            config=config,
        )
        if not text:
            continue
        client.send_message(config["device_id"], target, text)
        replies.append({"message_id": message.get("id"), "target": target, "text": text})
        state = read_agent_state()
        processed = set(state.get("processed") or [])
        processed.add(message.get("id"))
        state["processed"] = list(processed)
        state["last_run"] = _now_iso()
        state["replies"] = int(state.get("replies") or 0) + 1
        write_agent_state(state)
    return replies


def run_forever(config=None, stop_event=None):
    config = config or load_config()
    settings = agent_settings(config)
    if not read_credentials():
        login(agent_name=settings["name"])
    while True:
        if stop_event is not None and stop_event.is_set():
            return 0
        try:
            run_once(config)
        except (AgentError, ApiError, OSError) as error:
            print(f"agent error: {error}", flush=True)
        time.sleep(settings["poll_interval"])
