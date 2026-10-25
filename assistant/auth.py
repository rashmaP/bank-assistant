"""Stub for the bank's existing customer authentication.

The brief says customers are *already authenticated*. In production the assistant would
validate the bank's session/JWT. Here a tiny HMAC-signed token plays that role so the
rest of the system is built the way it would be in production: the customer identity
comes from a verified credential, never from the chat text or from the model.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class AuthError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(secret: str, customer_id: str, name: str, ttl: int = 3600, now: float | None = None) -> str:
    payload = _b64(json.dumps({"cid": customer_id, "name": name, "exp": int((time.time() if now is None else now) + ttl)}).encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(secret: str, token: str, now: float | None = None) -> dict:
    try:
        payload, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise AuthError("bad signature")
        claims = json.loads(_unb64(payload))
    except AuthError:
        raise
    except Exception as e:  # malformed token
        raise AuthError("malformed token") from e
    if claims.get("exp", 0) < (time.time() if now is None else now):
        raise AuthError("expired")
    return claims
