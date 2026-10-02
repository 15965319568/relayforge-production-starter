from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_payload(*, secret: str, timestamp: int, event_id: str, body: bytes) -> str:
    prefix = f"{timestamp}.".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), prefix + body, hashlib.sha256)
    return "v1=" + digest.hexdigest()


def build_headers(
    *,
    secret: str,
    timestamp: int,
    event_id: str,
    delivery_id: str,
    body: bytes,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Relay-Event-Id": event_id,
        "X-Relay-Delivery-Id": delivery_id,
        "X-Relay-Timestamp": str(timestamp),
        "X-Relay-Signature": sign_payload(
            secret=secret, timestamp=timestamp, event_id=event_id, body=body
        ),
        "Idempotency-Key": delivery_id,
        "User-Agent": "RelayForge/0.1",
    }
