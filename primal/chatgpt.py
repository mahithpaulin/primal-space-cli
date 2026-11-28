"""Sign in with ChatGPT (SIWC) for open-source clients.

Implements the documented OAuth flow:
  authorization code + PKCE against https://auth.openai.com with dynamic
  client registration (client_id=dynamic_agent_client), a stable
  ext_agent_host_id per installation, and ChatGPT plan usage scopes.
"""

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .config import STATE_DIR

AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
API_BASE = "https://api.openai.com/v1"
RESOURCE = "https://api.openai.com/v1"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
IDENTITY_SCOPES = "openid profile email"
PLAN_SCOPES = "offline_access resource.invoke chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"
ISSUER = "https://auth.openai.com"
DEBUG_LOG = STATE_DIR / "chatgpt_debug.log"

CREDENTIALS_FILE = STATE_DIR / "chatgpt_credentials.json"
HOST_ID_FILE = STATE_DIR / "agent_host_id"
PENDING_CLIENT_FILE = STATE_DIR / "chatgpt_client_id"


class OAuthError(RuntimeError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _random_urlsafe(length=32):
    return _b64url(secrets.token_bytes(length))


def host_id():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if HOST_ID_FILE.exists():
        value = HOST_ID_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"urn:uuid:{__import__('uuid').uuid4()}"
    HOST_ID_FILE.write_text(value + "\n", encoding="utf-8")
    try:
        HOST_ID_FILE.chmod(0o600)
    except OSError:
        pass
    return value


def credentials_path():
    return CREDENTIALS_FILE


def read_credentials():
    try:
        value = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_credentials(credentials):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = CREDENTIALS_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(credentials, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    os.replace(temp, CREDENTIALS_FILE)


def pending_client_id():
    try:
        return PENDING_CLIENT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_pending_client_id(value):
    if not value:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_CLIENT_FILE.write_text(value + "\n", encoding="utf-8")
    try:
        PENDING_CLIENT_FILE.chmod(0o600)
    except OSError:
        pass


def clear_credentials():
    removed = False
    for path in (CREDENTIALS_FILE, PENDING_CLIENT_FILE):
        try:
            path.unlink()
            removed = True
        except OSError:
            continue
    return removed


def revoke(record=None):
    record = record or read_credentials()
    if not record:
        return False
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        return False
    endpoint = "https://auth.openai.com/oauth/revoke"
    try:
        discovery = _fetch_json("https://auth.openai.com/.well-known/openid-configuration", timeout=20)
        endpoint = discovery.get("revocation_endpoint") or endpoint
    except OAuthError:
        pass
    try:
        _post_form(
            endpoint,
            {
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": record.get("client_id"),
            },
        )
        return True
    except OAuthError:
        return False


def _debug_enabled():
    return os.environ.get("PRIMAL_CHATGPT_DEBUG", "").strip() not in ("", "0", "false")


def _debug(message):
    if not _debug_enabled():
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _redact(value):
    text = str(value or "")
    if len(text) <= 8:
        return f"<len:{len(text)}>"
    return f"<len:{len(text)} sha1:{hashlib.sha256(text.encode()).hexdigest()[:12]}>"


def _parse_query(raw):
    params = {}
    for pair in raw.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        params[urllib.parse.unquote(key)] = urllib.parse.unquote(value)
    return params


def _post_form(url, fields, timeout=30):
    if url == TOKEN_URL:
        shown = {key: _redact(value) for key, value in fields.items()}
        shown["client_id"] = fields.get("client_id")
        shown["redirect_uri"] = fields.get("redirect_uri")
        shown["resource"] = fields.get("resource")
        shown["grant_type"] = fields.get("grant_type")
        _debug(f"token request url={url} fields={shown}")
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:600]
        request_id = error.headers.get("openai-request-id") or error.headers.get("x-request-id")
        if url == TOKEN_URL:
            _debug(f"token response status={error.code} request_id={request_id} body={detail}")
        raise OAuthError(f"token request failed ({error.code}): {detail}") from error
    except (urllib.error.URLError, OSError) as error:
        raise OAuthError(f"token request failed: {error}") from error
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise OAuthError("token endpoint returned invalid JSON") from error


def _fetch_json(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise OAuthError(f"request failed ({error.code}): {detail}") from error
    except (urllib.error.URLError, OSError) as error:
        raise OAuthError(f"request failed: {error}") from error
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise OAuthError("response was not valid JSON") from error


def _decode_segment(segment):
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding).decode("utf-8"))


def _sha256_prefix(signing_input, algorithm):
    if algorithm != "RS256":
        raise OAuthError(f"unsupported id token algorithm: {algorithm}")
    digest = hashlib.sha256(signing_input).digest()
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    return prefix + digest


def _rsa_pkcs1_v15_verify(signing_input, signature, jwk):
    """Verify an RS256 signature using only the standard library."""
    modulus = int.from_bytes(base64.urlsafe_b64decode(jwk["n"] + "=" * (-len(jwk["n"]) % 4)), "big")
    exponent = int.from_bytes(base64.urlsafe_b64decode(jwk["e"] + "=" * (-len(jwk["e"]) % 4)), "big")
    key_size = (modulus.bit_length() + 7) // 8
    if len(signature) != key_size:
        return False
    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(key_size, "big")
    expected = _sha256_prefix(signing_input, jwk.get("alg") or "RS256")
    padding_length = key_size - len(expected) - 3
    if padding_length < 8:
        return False
    target = bytes([0x00, 0x01]) + b"\xff" * padding_length + b"\x00" + expected
    return encoded == target


def _jwks_cache():
    return _fetch_json(JWKS_URL, timeout=20)


def validate_id_token(id_token, expected_audience, expected_nonce):
    if not id_token:
        raise OAuthError("token response did not include an id token")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OAuthError("id token is malformed")
    header = _decode_segment(parts[0])
    claims = _decode_segment(parts[1])
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    if header.get("alg") != "RS256":
        raise OAuthError("id token uses an unsupported algorithm")
    keyset = _jwks_cache()
    matched = None
    for key in keyset.get("keys", []):
        if key.get("alg") not in (None, "RS256"):
            continue
        if key.get("kid") and header.get("kid") and key.get("kid") != header.get("kid"):
            continue
        matched = key
        break
    if not matched:
        raise OAuthError("no matching signing key for the id token")
    key = dict(matched)
    signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    if not _rsa_pkcs1_v15_verify(signing_input, signature, key):
        raise OAuthError("id token signature verification failed")
    if claims.get("iss") != ISSUER:
        raise OAuthError("id token issuer is not OpenAI")
    if expected_audience and claims.get("aud") != expected_audience:
        raise OAuthError("id token audience does not match the issued client id")
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and expires < time.time():
        raise OAuthError("id token has expired")
    if expected_nonce and claims.get("nonce") != expected_nonce:
        raise OAuthError("id token nonce does not match the request")
    return claims


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _CallbackHandler(BaseHTTPRequestHandler):
    result = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        raw_query = parsed.query
        query = _parse_query(raw_query)
        _debug(
            f"callback raw_query={raw_query[:200]!r} keys={sorted(query)} "
            f"code={_redact(query.get('code'))} client_id={query.get('client_id')!r} "
            f"scope={query.get('scope')!r} error={query.get('error')!r}"
        )
        type(self).result = dict(query, _raw_query=raw_query)
        body = b"<html><body><h3>PrimalSpace</h3><p>Sign in complete. You can close this tab.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def authorization_url(agent_name, client_id=None, id_token=None, login_hint=None, port=None):
    verifier = _random_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _random_urlsafe(24)
    nonce = _random_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{port or _free_port()}{CALLBACK_PATH}"
    params = {
        "response_type": "code",
        "client_id": client_id or DYNAMIC_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": f"{IDENTITY_SCOPES} {PLAN_SCOPES}",
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ext_agent_host_id": host_id(),
    }
    if not client_id:
        params["agent_name_hint"] = agent_name or "PrimalSpace"
    if id_token:
        params["id_token_hint"] = id_token
    if login_hint:
        params["login_hint"] = login_hint
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "url": url,
        "verifier": verifier,
        "state": state,
        "nonce": nonce,
        "redirect_uri": redirect_uri,
    }


def _wait_for_callback(server, expected_state, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _CallbackHandler.result
        if result:
            _CallbackHandler.result = None
            if result.get("error"):
                raise OAuthError(f"authorization failed: {result.get('error')}")
            if result.get("state") != expected_state:
                raise OAuthError("authorization state mismatch")
            if not result.get("code"):
                raise OAuthError("authorization response did not include a code")
            return result
        time.sleep(0.25)
    raise OAuthError("timed out waiting for the browser sign-in")


def login(agent_name="PrimalSpace", open_browser=True, timeout=900, on_url=None, attempts=3):
    """Run the SIWC flow and return the stored credential record."""
    saved = read_credentials() or {}
    client_id = saved.get("client_id") or pending_client_id() or None
    last_error = None
    for attempt in range(max(1, attempts)):
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        request = authorization_url(
            agent_name,
            client_id=client_id,
            id_token=saved.get("id_token"),
            login_hint=saved.get("email"),
            port=server.server_address[1],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if on_url is not None:
                try:
                    on_url(request["url"])
                except Exception:
                    pass
            if open_browser:
                try:
                    webbrowser.open(request["url"])
                except Exception:
                    pass
            callback = _wait_for_callback(server, request["state"], timeout=timeout)
        finally:
            server.shutdown()
            server.server_close()
        issued_client_id = callback.get("client_id") or client_id
        if not issued_client_id:
            raise OAuthError("authorization response did not include an issued client id")
        if client_id and callback.get("client_id") and callback.get("client_id") != client_id:
            raise OAuthError("authorization returned a different client id than the saved registration")
        set_pending_client_id(issued_client_id)
        client_id = issued_client_id
        try:
            token = _post_form(
                TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "client_id": issued_client_id,
                    "code": callback["code"],
                    "code_verifier": request["verifier"],
                    "redirect_uri": request["redirect_uri"],
                    "resource": RESOURCE,
                },
            )
        except OAuthError as error:
            if "invalid_grant" in str(error) and attempt + 1 < max(1, attempts):
                last_error = error
                continue
            raise
        granted = str(token.get("scope") or "").split()
        if "chatgpt.tokens.use.direct" not in granted:
            raise OAuthError("the account did not grant ChatGPT plan usage")
        claims = validate_id_token(token.get("id_token"), issued_client_id, request["nonce"])
        record = {
            "client_id": issued_client_id,
            "ext_agent_host_id": host_id(),
            "expires_at": time.time() + int(token.get("expires_in") or 3600),
            "id_token": token.get("id_token"),
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
            "token_type": token.get("token_type") or "Bearer",
            "expires_in": int(token.get("expires_in") or 3600),
            "scopes": granted,
            "email": claims.get("email") or saved.get("email") or "",
            "subject": claims.get("sub") or "",
            "issuer": claims.get("iss") or ISSUER,
            "saved_at": _now_iso(),
        }
        write_credentials(record)
        return record
    raise last_error or OAuthError("sign-in did not complete")


def refresh(credentials=None):
    record = credentials or read_credentials()
    if not record:
        raise OAuthError("not signed in with ChatGPT")
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise OAuthError("the saved session has no refresh token")
    token = _post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": record.get("client_id"),
            "refresh_token": refresh_token,
            "resource": RESOURCE,
        },
    )
    granted = str(token.get("scope") or record.get("scopes") or "").split()
    updated = dict(record)
    updated["access_token"] = token.get("access_token") or record.get("access_token")
    updated["refresh_token"] = token.get("refresh_token") or refresh_token
    updated["id_token"] = token.get("id_token") or record.get("id_token")
    updated["expires_in"] = int(token.get("expires_in") or 3600)
    updated["scopes"] = granted
    updated["expires_at"] = time.time() + int(token.get("expires_in") or 3600)
    updated["saved_at"] = _now_iso()
    write_credentials(updated)
    return updated


def access_token(credentials=None):
    record = credentials or read_credentials()
    if not record:
        raise OAuthError("not signed in with ChatGPT")
    expires_at = record.get("expires_at")
    if not expires_at:
        saved = _parse_time(record.get("saved_at"))
        expires_at = saved + int(record.get("expires_in") or 3600)
    if expires_at - time.time() > 120:
        if not record.get("access_token"):
            raise OAuthError("the saved session has no access token")
        return record["access_token"], record
    return refresh(record)["access_token"], record


def _parse_time(value):
    if not value:
        return time.time()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def status():
    record = read_credentials()
    if not record:
        return {"connected": False}
    expires_at = record.get("expires_at")
    if not expires_at:
        expires_at = _parse_time(record.get("saved_at")) + int(record.get("expires_in") or 3600)
    return {
        "connected": True,
        "email": record.get("email") or "",
        "client_id": record.get("client_id") or "",
        "host_id": record.get("ext_agent_host_id") or "",
        "scopes": record.get("scopes") or [],
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        "expired": expires_at < time.time(),
        "has_refresh_token": bool(record.get("refresh_token")),
        "saved_at": record.get("saved_at"),
    }


def list_models(token=None):
    token = token or access_token()[0]
    payload = _fetch_json(f"{API_BASE}/models", {"Authorization": f"Bearer {token}"}, timeout=30)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        models = payload.get("data") if isinstance(payload, dict) else []
    result = []
    for model in models or []:
        if not isinstance(model, dict):
            continue
        if model.get("visibility") not in (None, "list"):
            continue
        slug = model.get("slug") or model.get("id")
        if not slug:
            continue
        result.append({"slug": slug, "display_name": model.get("display_name") or slug})
    return result
