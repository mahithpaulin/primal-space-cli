import ast
import base64
import binascii
import builtins
import collections
import copy
import datetime
import decimal
import fractions
import functools
import hashlib
import hmac
import io
import itertools
import json
import math
import operator
import os
import random
import re
import signal
import statistics
import string
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import time as _time_module
import urllib.error
import urllib.request
import uuid
import zlib
from types import SimpleNamespace

try:
    import resource
except ImportError:
    resource = None

MAX_CODE_BYTES = 65536
MAX_ARGS_BYTES = 8192
MAX_OUTPUT_BYTES = 65536
MAX_RESULT_BYTES = 8192
MAX_SECONDS = 30
MAX_HTTP_RESPONSE = 32768

FORBIDDEN_NAMES = {
    "__builtins__",
    "__import__",
    "breakpoint",
    "classmethod",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "object",
    "open",
    "property",
    "quit",
    "setattr",
    "staticmethod",
    "super",
    "type",
    "vars",
}

SAFE_MODULES = {
    "base64": base64,
    "binascii": binascii,
    "collections": collections,
    "copy": copy,
    "datetime": datetime,
    "decimal": decimal,
    "fractions": fractions,
    "functools": functools,
    "hashlib": hashlib,
    "hmac": hmac,
    "itertools": itertools,
    "json": json,
    "math": math,
    "operator": operator,
    "random": random,
    "re": re,
    "statistics": statistics,
    "string": string,
    "struct": struct,
    "textwrap": textwrap,
    "time": _time_module,
    "uuid": uuid,
    "zlib": zlib,
}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level:
        raise ImportError("relative imports are not allowed")
    root = str(name).split(".")[0]
    if root not in SAFE_MODULES:
        raise ImportError(f"module is not allowed: {root}")
    return SAFE_MODULES[root]


SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs",
        "all",
        "any",
        "ascii",
        "bin",
        "bool",
        "bytes",
        "callable",
        "chr",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "hash",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "AssertionError",
        "ArithmeticError",
        "Exception",
        "IndexError",
        "KeyError",
        "LookupError",
        "NameError",
        "RuntimeError",
        "StopIteration",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
    )
}

SAFE_BUILTINS["__import__"] = _safe_import

SAFE_MATH = SimpleNamespace(
    ceil=math.ceil,
    cos=math.cos,
    e=math.e,
    exp=math.exp,
    factorial=math.factorial,
    floor=math.floor,
    fabs=math.fabs,
    gcd=math.gcd,
    inf=math.inf,
    isfinite=math.isfinite,
    log=math.log,
    log10=math.log10,
    pi=math.pi,
    sin=math.sin,
    sqrt=math.sqrt,
    tan=math.tan,
)


class SandboxError(Exception):
    pass


class SandboxTimeout(SandboxError):
    pass


class SandboxCancelled(SandboxError):
    pass


def _clip(value, limit):
    return str(value)[:limit]


def _validate_json_size(value, limit, label):
    try:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise SandboxError(f"{label} must be JSON-serializable") from error
    if len(encoded.encode("utf-8")) > limit:
        raise SandboxError(f"{label} exceeds {limit} bytes")


def validate_code(code):
    if not isinstance(code, str) or not code.strip():
        raise SandboxError("code must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise SandboxError(f"code exceeds {MAX_CODE_BYTES} bytes")
    try:
        tree = ast.parse(code, filename="<primal>", mode="exec")
    except SyntaxError as error:
        raise SandboxError(f"SyntaxError: {error}") from error

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in SAFE_MODULES:
                    raise SandboxError(f"module is not allowed: {root}")
        if isinstance(node, ast.ImportFrom):
            if node.level:
                raise SandboxError("relative imports are not allowed")
            root = (node.module or "").split(".")[0]
            if root not in SAFE_MODULES:
                raise SandboxError(f"module is not allowed: {root}")
            for alias in node.names:
                if alias.name.startswith("_"):
                    raise SandboxError("private imports are not allowed")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise SandboxError(f"name is not allowed: {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise SandboxError("private and dunder attributes are not allowed")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(part.startswith("__") and part.endswith("__") for part in node.value.split(".")):
                raise SandboxError("dunder names are not allowed")
    return tree


def _safe_result(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        value = repr(value)
        encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= MAX_RESULT_BYTES:
        return value
    return {"truncated": True, "preview": _clip(encoded, MAX_RESULT_BYTES)}


def _apply_limits(cpu_seconds, memory_mb):
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (OSError, ValueError):
        pass
    try:
        memory_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (OSError, ValueError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    except (OSError, ValueError):
        pass


def _alarm_handler(_signum, _frame):
    raise SandboxTimeout("execution timed out")


def _child_payload(payload_path):
    with open(payload_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    try:
        os.unlink(payload_path)
    except OSError:
        pass
    return payload


def child_main(payload_path):
    payload = _child_payload(payload_path)
    code = payload["code"]
    args = payload.get("args")
    max_seconds = int(payload["max_seconds"])
    memory_mb = int(payload["memory_mb"])
    cpu_seconds = int(payload["cpu_seconds"])
    output = io.StringIO()
    error_output = io.StringIO()
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    sys.stdout = output
    sys.stderr = error_output
    response = None
    try:
        validate_code(code)
        _apply_limits(cpu_seconds, memory_mb)
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(max_seconds)
        namespace = {"__builtins__": SAFE_BUILTINS, "math": SAFE_MATH}
        exec(compile(code, "<primal>", "exec"), namespace, namespace)
        function = namespace.get("main")
        result = function(args) if callable(function) else None
        response = {
            "ok": True,
            "result": _safe_result(result),
            "stdout": _clip(output.getvalue(), MAX_OUTPUT_BYTES),
            "stderr": _clip(error_output.getvalue(), MAX_OUTPUT_BYTES),
        }
    except SandboxTimeout as error:
        response = {
            "ok": False,
            "timeout": True,
            "error": str(error),
            "stdout": _clip(output.getvalue(), MAX_OUTPUT_BYTES),
            "stderr": _clip(error_output.getvalue(), MAX_OUTPUT_BYTES),
        }
    except BaseException as error:
        response = {
            "ok": False,
            "timeout": False,
            "error": f"{type(error).__name__}: {error}"[:500],
            "stdout": _clip(output.getvalue(), MAX_OUTPUT_BYTES),
            "stderr": _clip(error_output.getvalue(), MAX_OUTPUT_BYTES),
        }
    finally:
        signal.alarm(0)
        sys.stdout = real_stdout
        sys.stderr = real_stderr
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()
    return 0


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_python(code, args=None, max_seconds=10, memory_mb=128, cpu_seconds=None):
    validate_code(code)
    _validate_json_size(args, MAX_ARGS_BYTES, "args")
    max_seconds = max(1, min(MAX_SECONDS, int(max_seconds)))
    memory_mb = max(16, min(4096, int(memory_mb)))
    cpu_seconds = max(1, min(max_seconds, int(cpu_seconds or max_seconds)))
    payload = {
        "code": code,
        "args": args,
        "max_seconds": max_seconds,
        "memory_mb": memory_mb,
        "cpu_seconds": cpu_seconds,
    }
    fd, payload_path = tempfile.mkstemp(prefix="primal-python-", suffix=".json")
    os.close(fd)
    try:
        os.chmod(payload_path, 0o600)
        with open(payload_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        process = subprocess.Popen(
            [sys.executable, "-c", "from primal.sandbox import child_main; raise SystemExit(child_main(__import__('sys').argv[1]))", payload_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        started = time.monotonic()
        try:
            stdout, stderr = process.communicate(timeout=max_seconds + 2)
        except subprocess.TimeoutExpired as error:
            _terminate(process)
            raise SandboxTimeout("execution timed out") from error
        duration_ms = round((time.monotonic() - started) * 1000)
        if process.returncode != 0:
            if process.returncode < 0 and -process.returncode in {
                getattr(signal, "SIGXCPU", -1),
                getattr(signal, "SIGALRM", -1),
            }:
                raise SandboxTimeout("execution timed out")
            message = stderr.decode("utf-8", "replace").strip() or "sandbox process failed"
            raise SandboxError(_clip(message, 500))
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SandboxError("sandbox returned invalid output") from error
        if not response.get("ok"):
            if response.get("timeout"):
                raise SandboxTimeout(response.get("error") or "execution timed out")
            raise SandboxError(response.get("error") or "python execution failed")
        response["duration_ms"] = duration_ms
        return response
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(child_main(sys.argv[1]))


def _apply_shell_limits(cpu_seconds, memory_mb):
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (OSError, ValueError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    except (OSError, ValueError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (OSError, ValueError):
        pass
    try:
        os.setsid()
    except OSError:
        pass


def run_shell(command, items=None, shard_index=0, shard_count=1, max_seconds=10, memory_mb=128, cpu_seconds=None):
    if not isinstance(command, str) or not command.strip():
        raise SandboxError("command must be a non-empty string")
    if len(command.encode("utf-8")) > 8000:
        raise SandboxError("command is too large")
    max_seconds = max(1, min(MAX_SECONDS, int(max_seconds)))
    memory_mb = max(16, min(4096, int(memory_mb)))
    cpu_seconds = max(1, min(max_seconds, int(cpu_seconds or max_seconds)))
    _validate_json_size(items, MAX_ARGS_BYTES, "items")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="primal-shell-") as workdir:
        environment = {
            "PATH": os.environ.get("PATH", "/system/bin:/usr/bin:/bin"),
            "HOME": workdir,
            "TMPDIR": workdir,
            "LANG": "C.UTF-8",
            "PRIMAL_ITEMS": json.dumps(items, ensure_ascii=False, separators=(",", ":")),
            "PRIMAL_SHARD_INDEX": str(int(shard_index)),
            "PRIMAL_SHARD_COUNT": str(int(shard_count)),
        }
        process = subprocess.Popen(
            ["sh", "-c", command],
            cwd=workdir,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            preexec_fn=lambda: _apply_shell_limits(cpu_seconds, memory_mb),
        )
        try:
            stdout, stderr = process.communicate(timeout=max_seconds + 2)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.communicate()
            raise SandboxTimeout("shell command timed out") from error
        duration_ms = round((time.monotonic() - started) * 1000)
        return {
            "exit_code": process.returncode,
            "stdout": _clip(stdout.decode("utf-8", "replace"), MAX_OUTPUT_BYTES),
            "stderr": _clip(stderr.decode("utf-8", "replace"), MAX_OUTPUT_BYTES),
            "duration_ms": duration_ms,
        }


def run_http(params, timeout_s=None):
    url = params["url"]
    method = params["method"]
    headers = dict(params.get("headers") or {})
    headers.setdefault("User-Agent", "PrimalSpace/1.0")
    body = params.get("body")
    data = body.encode("utf-8") if body is not None else None
    timeout = max(1, min(30, int(timeout_s or params.get("timeout_s") or 20)))
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read(MAX_HTTP_RESPONSE + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read(MAX_HTTP_RESPONSE + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SandboxError(f"http request failed: {error}") from error
    truncated = len(raw) > MAX_HTTP_RESPONSE
    text = raw[:MAX_HTTP_RESPONSE].decode("utf-8", "replace")
    return {
        "status": status,
        "body": text,
        "truncated": truncated,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
