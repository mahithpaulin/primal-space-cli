import json

WORKLOADS = ("python", "shell", "http")
MAX_PYTHON_CODE = 65536
MAX_ITEMS = 512
MAX_ITEMS_BYTES = 16384
MAX_SHELL_COMMAND = 4000
MAX_HTTP_BODY = 16384


def _normalize_items(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("items must be an array")
    if len(value) > MAX_ITEMS:
        raise ValueError(f"items must contain at most {MAX_ITEMS} entries")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_ITEMS_BYTES:
        raise ValueError(f"items exceeds {MAX_ITEMS_BYTES} bytes")
    return value


def normalize_python_params(params):
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_PYTHON_CODE:
        raise ValueError(f"code exceeds {MAX_PYTHON_CODE} bytes")
    items = _normalize_items(params.get("items"))
    args = params["args"] if "args" in params else items
    try:
        encoded = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("args must be JSON-serializable") from error
    if len(encoded.encode("utf-8")) > MAX_ITEMS_BYTES:
        raise ValueError(f"args exceeds {MAX_ITEMS_BYTES} bytes")
    return {"code": code, "args": args, "items": items}


def normalize_shell_params(params):
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    command = params.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if len(command.encode("utf-8")) > MAX_SHELL_COMMAND:
        raise ValueError(f"command exceeds {MAX_SHELL_COMMAND} bytes")
    return {"command": command, "items": _normalize_items(params.get("items"))}


def normalize_http_params(params):
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    method = str(params.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ValueError("method is invalid")
    url = params.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("url must be an http(s) URL")
    headers = params.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    body = params.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("body must be a string")
    try:
        timeout_s = int(params.get("timeout_s") or 20)
    except (TypeError, ValueError):
        timeout_s = 20
    return {
        "method": method,
        "url": url,
        "headers": {str(key): str(value) for key, value in headers.items()},
        "body": body,
        "timeout_s": max(1, min(30, timeout_s)),
    }
