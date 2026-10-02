from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Mapping

from .errors import ValidationError


CURSOR_VERSION = 1


class CursorCodec:
    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("cursor secret is required")
        self._secret = secret.encode("utf-8")

    def encode(
        self,
        *,
        scope: str,
        position: Mapping[str, Any],
        query: Mapping[str, Any],
        expires_at: datetime,
    ) -> str:
        payload = {
            "v": CURSOR_VERSION,
            "scope": scope,
            "position": dict(position),
            "query": None,
            "exp": int(expires_at.astimezone(UTC).timestamp()),
        }
        raw = _canonical(payload)
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return _b64(raw) + "." + _b64(signature)

    def decode(
        self,
        token: str,
        *,
        scope: str,
        query: Mapping[str, Any],
        now: datetime,
    ) -> Mapping[str, Any]:
        try:
            payload_part, signature_part = token.split(".", 1)
            raw = _unb64(payload_part)
            supplied = _unb64(signature_part)
        except (ValueError, UnicodeError) as exc:
            raise ValidationError("malformed cursor") from exc
        expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            raise ValidationError("cursor signature is invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("cursor payload is invalid") from exc
        if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
            raise ValidationError("cursor version is unsupported")
        if payload.get("scope") != scope:
            raise ValidationError("cursor scope does not match request")
        expiry = payload.get("exp")
        if not isinstance(expiry, int) or int(now.astimezone(UTC).timestamp()) > expiry:
            raise ValidationError("cursor has expired")
        position = payload.get("position")
        if not isinstance(position, dict):
            raise ValidationError("cursor position is invalid")
        return position


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(query: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(query))).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
