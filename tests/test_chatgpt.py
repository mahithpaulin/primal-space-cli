import base64
import hashlib
import json
import random
import time
import unittest
from unittest import mock

from primal import chatgpt


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _is_probable_prime(number, rounds=24):
    if number < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if number % small == 0:
            return number == small
    divisor = number - 1
    exponent = 0
    while divisor % 2 == 0:
        divisor //= 2
        exponent += 1
    for _ in range(rounds):
        base = random.randrange(2, number - 1)
        value = pow(base, divisor, number)
        if value in (1, number - 1):
            continue
        for _ in range(exponent - 1):
            value = pow(value, 2, number)
            if value == number - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        candidate = random.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def _rsa_key(bits=1024):
    half = bits // 2
    while True:
        p = _prime(half)
        q = _prime(half)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        e = 65537
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return {"n": n, "e": e, "d": d, "size": (bits + 7) // 8}


def _sign(key, signing_input):
    digest = hashlib.sha256(signing_input).digest()
    prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    expected = prefix + digest
    padding_length = key["size"] - len(expected) - 3
    block = bytes([0x00, 0x01]) + b"\xff" * padding_length + b"\x00" + expected
    signature = pow(int.from_bytes(block, "big"), key["d"], key["n"])
    return signature.to_bytes(key["size"], "big")


def _id_token(key, claims, kid="test-key"):
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}".encode()
    signature = _sign(key, signing_input)
    return f"{signing_input.decode()}.{_b64(signature)}"


class ChatGPTOAuthTests(unittest.TestCase):
    def test_authorization_url_contains_required_parameters(self):
        request = chatgpt.authorization_url("Primal Agent", port=1455)
        query = request["url"].split("?", 1)[1]
        params = dict(part.split("=", 1) for part in query.split("&"))
        self.assertTrue(request["url"].startswith(chatgpt.AUTHORIZE_URL))
        self.assertEqual(params["client_id"], "dynamic_agent_client")
        self.assertEqual(params["agent_name_hint"], "Primal+Agent")
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["redirect_uri"], "http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback")
        self.assertEqual(params["resource"], "https%3A%2F%2Fapi.openai.com%2Fv1")
        self.assertEqual(params["scope"], "openid+profile+email+offline_access+resource.invoke+chatgpt.tokens.use.direct")
        self.assertTrue(params["ext_agent_host_id"].startswith("urn%3Auuid%3A"))
        expected = _b64(hashlib.sha256(request["verifier"].encode()).digest())
        self.assertEqual(params["code_challenge"], expected)

    def test_authorization_url_omits_agent_name_hint_for_saved_clients(self):
        request = chatgpt.authorization_url("Primal Agent", client_id="client-123", port=1455)
        self.assertNotIn("agent_name_hint", request["url"])
        self.assertIn("client_id=client-123", request["url"])

    def test_id_token_signature_validation(self):
        key = _rsa_key()
        jwk = {
            "kty": "RSA",
            "kid": "test-key",
            "alg": "RS256",
            "n": _b64(key["n"].to_bytes(key["size"], "big")),
            "e": _b64(key["e"].to_bytes(3, "big")),
        }
        claims = {
            "iss": chatgpt.ISSUER,
            "aud": "client-123",
            "sub": "user-1",
            "email": "user@example.com",
            "nonce": "nonce-1",
            "exp": int(time.time()) + 3600,
        }
        token = _id_token(key, claims)
        with mock.patch.object(chatgpt, "_jwks_cache", return_value={"keys": [jwk]}):
            validated = chatgpt.validate_id_token(token, "client-123", "nonce-1")
        self.assertEqual(validated["email"], "user@example.com")

        with mock.patch.object(chatgpt, "_jwks_cache", return_value={"keys": [jwk]}):
            with self.assertRaises(chatgpt.OAuthError):
                chatgpt.validate_id_token(token, "client-123", "wrong-nonce")
            with self.assertRaises(chatgpt.OAuthError):
                chatgpt.validate_id_token(token, "other-client", "nonce-1")

    def test_id_token_rejects_tampered_payload(self):
        key = _rsa_key()
        jwk = {
            "kty": "RSA",
            "kid": "test-key",
            "alg": "RS256",
            "n": _b64(key["n"].to_bytes(key["size"], "big")),
            "e": _b64(key["e"].to_bytes(3, "big")),
        }
        claims = {
            "iss": chatgpt.ISSUER,
            "aud": "client-123",
            "nonce": "nonce-1",
            "exp": int(time.time()) + 3600,
        }
        token = _id_token(key, claims)
        header, payload, signature = token.split(".")
        tampered = f"{header}.{_b64(json.dumps({**claims, 'sub': 'attacker'}).encode())}.{signature}"
        with mock.patch.object(chatgpt, "_jwks_cache", return_value={"keys": [jwk]}):
            with self.assertRaises(chatgpt.OAuthError):
                chatgpt.validate_id_token(tampered, "client-123", "nonce-1")

    def test_callback_state_mismatch_is_rejected(self):
        handler = chatgpt._CallbackHandler
        handler.result = {"code": "abc", "state": "wrong"}
        with self.assertRaises(chatgpt.OAuthError):
            chatgpt._wait_for_callback(None, "expected", timeout=1)
        handler.result = None

    def test_credentials_round_trip(self):
        with mock.patch.object(chatgpt, "CREDENTIALS_FILE", chatgpt.STATE_DIR / "test-credentials.json"):
            chatgpt.write_credentials({"access_token": "token", "client_id": "client"})
            self.assertEqual(chatgpt.read_credentials()["access_token"], "token")
            chatgpt.clear_credentials()
            self.assertIsNone(chatgpt.read_credentials())


if __name__ == "__main__":
    unittest.main()
