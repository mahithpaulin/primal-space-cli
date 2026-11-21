import json
import urllib.error
import urllib.request

from . import __version__

_UNSET = object()


class ApiError(Exception):
    def __init__(self, status, message, payload=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


class ApiClient:
    def __init__(self, server_url, token=None, timeout=20):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method, path, payload=None, token=_UNSET, admin_key=None, timeout=None):
        url = f"{self.server_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": f"primal-cli/{__version__}",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        auth_token = self.token if token is _UNSET else token
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        if admin_key:
            headers["X-Primal-Admin-Key"] = admin_key

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {}
            message = parsed.get("error") or f"HTTP {error.code}"
            raise ApiError(error.code, message, parsed) from error
        except urllib.error.URLError as error:
            raise ApiError(None, f"network error: {error.reason}") from error
        except TimeoutError as error:
            raise ApiError(None, "request timed out") from error

        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(None, "invalid JSON from server") from error

    def register(self, enrollment_key, install_id, device):
        return self.request(
            "POST",
            "/api/v1/register",
            {
                "enrollment_key": enrollment_key,
                "install_id": install_id,
                "device": device,
            },
            token=None,
        )

    def heartbeat(self, device_id, metrics, worker, cli_version):
        return self.request(
            "POST",
            "/api/v1/heartbeat",
            {
                "device_id": device_id,
                "metrics": metrics,
                "worker": worker,
                "cli_version": cli_version,
            },
        )

    def disconnect(self, device_id):
        return self.request("POST", "/api/v1/disconnect", {"device_id": device_id})

    def get_devices(self):
        return self.request("GET", "/api/v1/devices", token=None)

    def get_device(self, device_id):
        return self.request("GET", f"/api/v1/devices/{device_id}", token=None)

    def claim(self, device_id, compute_enabled):
        return self.request(
            "POST",
            "/api/v1/tasks/claim",
            {"device_id": device_id, "compute_enabled": compute_enabled},
        )

    def get_task(self, task_id):
        return self.request("GET", f"/api/v1/tasks/{task_id}", token=None)

    def submit_result(
        self,
        device_id,
        task_id,
        status,
        result=None,
        duration_ms=None,
        error=None,
        metrics=None,
        stdout=None,
    ):
        payload = {
            "device_id": device_id,
            "task_id": task_id,
            "status": status,
            "duration_ms": duration_ms,
            "error": error,
            "metrics": metrics,
        }
        if result is not None:
            payload["result"] = result
        if stdout is not None:
            payload["stdout"] = stdout
        return self.request("POST", "/api/v1/tasks/result", payload)

    def create_task(self, device_id, params, resource_limits, admin_key):
        return self.request(
            "POST",
            "/api/v1/tasks",
            {
                "device_id": device_id,
                "workload": "hash_sum_v1",
                "params": params,
                "resource_limits": resource_limits,
            },
            token=None,
            admin_key=admin_key,
        )

    def cancel_task(self, task_id, admin_key):
        return self.request(
            "POST",
            f"/api/v1/tasks/{task_id}",
            {},
            token=None,
            admin_key=admin_key,
        )
