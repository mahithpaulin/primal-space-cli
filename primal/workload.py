import hashlib
import json
import re

MOD = 1_000_000_007
WORKLOAD = "hash_sum_v1"
PYTHON_WORKLOAD = "python_func"
MAX_ITERATIONS = 500_000
MAX_CHUNKS = 64
MAX_PYTHON_CODE = 65536
MAX_PYTHON_ARGS = 8192
SEED_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")


def normalize_params(params):
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    seed = params.get("seed")
    if not isinstance(seed, str) or not SEED_PATTERN.match(seed):
        raise ValueError("seed must match [A-Za-z0-9:_-]{1,64}")
    try:
        iterations = int(params.get("iterations"))
        chunks = int(params.get("chunks"))
    except (TypeError, ValueError) as error:
        raise ValueError("iterations and chunks must be integers") from error
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be 1..{MAX_ITERATIONS}")
    if not 1 <= chunks <= MAX_CHUNKS:
        raise ValueError(f"chunks must be 1..{MAX_CHUNKS}")
    return {"seed": seed, "iterations": iterations, "chunks": chunks}


def normalize_python_params(params):
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_PYTHON_CODE:
        raise ValueError(f"code exceeds {MAX_PYTHON_CODE} bytes")
    args = params.get("args")
    try:
        encoded = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("args must be JSON-serializable") from error
    if len(encoded.encode("utf-8")) > MAX_PYTHON_ARGS:
        raise ValueError(f"args exceeds {MAX_PYTHON_ARGS} bytes")
    return {"code": code, "args": args}


def chunk_bounds(iterations, chunks):
    chunk_size = -(-iterations // chunks)
    for index in range(chunks):
        start = index * chunk_size
        end = min(iterations, start + chunk_size)
        yield index, start, end


def compute_chunk(seed, start, end):
    total = 0
    for index in range(start, end):
        digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
        total = (total + int.from_bytes(digest[:8], "big")) % MOD
    return total


def hash_sum(params):
    normalized = normalize_params(params)
    seed = normalized["seed"]
    iterations = normalized["iterations"]
    chunks = normalized["chunks"]
    chunk_values = []
    total = 0
    for _index, start, end in chunk_bounds(iterations, chunks):
        value = compute_chunk(seed, start, end)
        chunk_values.append(value)
        total = (total + value) % MOD
    return {"value": total, "chunk_values": chunk_values}
