from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from datetime import timedelta

from _support import BASE_TIME
from relayforge.retry import RetryPolicy
from relayforge.signing import build_headers, canonical_json, sign_payload


class RetryPolicyTests(unittest.TestCase):
    def test_backoff_is_stable_across_calls(self) -> None:
        policy = RetryPolicy()
        self.assertEqual(
            policy.backoff_seconds("dlv-123", 3),
            policy.backoff_seconds("dlv-123", 3),
        )

    def test_backoff_stays_inside_symmetric_jitter_band(self) -> None:
        policy = RetryPolicy(base_seconds=10, multiplier=2, jitter_ratio=0.25)
        value = policy.backoff_seconds("delivery", 3)
        self.assertGreaterEqual(value, 30)
        self.assertLessEqual(value, 50)

    def test_backoff_caps_before_jitter(self) -> None:
        policy = RetryPolicy(base_seconds=100, cap_seconds=150, jitter_ratio=0)
        self.assertEqual(policy.backoff_seconds("delivery", 7), 150)

    def test_retry_after_accepts_seconds_and_caps_large_value(self) -> None:
        policy = RetryPolicy(max_retry_after_seconds=90)
        self.assertEqual(policy.retry_after_seconds("12.5", now=BASE_TIME), 12.5)
        self.assertEqual(policy.retry_after_seconds("900", now=BASE_TIME), 90)

    def test_retry_after_accepts_http_date(self) -> None:
        policy = RetryPolicy()
        header = "Sun, 20 Sep 2026 12:02:00 GMT"
        self.assertEqual(policy.retry_after_seconds(header, now=BASE_TIME), 120)

    def test_retry_after_ignores_invalid_or_past_values(self) -> None:
        policy = RetryPolicy()
        self.assertIsNone(policy.retry_after_seconds("later", now=BASE_TIME))
        self.assertIsNone(
            policy.retry_after_seconds("Sun, 20 Sep 2026 11:59:00 GMT", now=BASE_TIME)
        )

    def test_retry_window_is_inclusive_at_boundary(self) -> None:
        policy = RetryPolicy(max_attempts=3, max_elapsed_seconds=60)
        self.assertTrue(
            policy.permits_retry(
                attempt_ordinal=2,
                first_attempt_at=BASE_TIME,
                scheduled_at=BASE_TIME + timedelta(seconds=60),
            )
        )
        self.assertFalse(
            policy.permits_retry(
                attempt_ordinal=3,
                first_attempt_at=BASE_TIME,
                scheduled_at=BASE_TIME + timedelta(seconds=20),
            )
        )


class SigningTests(unittest.TestCase):
    def test_canonical_json_is_utf8_sorted_and_compact(self) -> None:
        body = canonical_json({"z": 1, "greeting": "你好", "nested": {"b": 2, "a": 1}})
        self.assertEqual(
            body,
            '{"greeting":"你好","nested":{"a":1,"b":2},"z":1}'.encode("utf-8"),
        )

    def test_canonical_json_rejects_nan(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"bad": float("nan")})

    def test_signature_matches_documented_wire_bytes(self) -> None:
        body = b'{"amount":42}'
        actual = sign_payload(
            secret="secret", timestamp=123, event_id="evt-1", body=body
        )
        expected = "v1=" + hmac.new(
            b"secret", b"123.evt-1." + body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(actual, expected)

    def test_headers_keep_delivery_as_stable_idempotency_key(self) -> None:
        headers = build_headers(
            secret="secret",
            timestamp=123,
            event_id="evt-1",
            delivery_id="dlv-1",
            body=b"{}",
        )
        self.assertEqual(headers["Idempotency-Key"], "dlv-1")
        self.assertEqual(headers["X-Relay-Delivery-Id"], "dlv-1")
        self.assertEqual(headers["X-Relay-Timestamp"], "123")


if __name__ == "__main__":
    unittest.main()

