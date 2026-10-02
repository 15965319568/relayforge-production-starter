from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from _enterprise_support import EnterpriseCase
from _support import BASE_TIME
from relayforge.errors import ValidationError
from relayforge.rate_limit import PersistentTokenBucket
from relayforge.secrets import SecretRing


class SecretRotationTests(EnterpriseCase, unittest.TestCase):
    def test_backfilled_secret_is_available_for_signing(self) -> None:
        secret = self.platform.secrets.signing_secret("ep-a", now=BASE_TIME)
        self.assertEqual((secret.key_id, secret.secret), ("v1", "secret-a"))

    def test_rotation_uses_new_key_for_signing_and_old_key_for_overlap(self) -> None:
        old_key, old_signature = self.platform.secrets.sign(
            "ep-a",
            now=BASE_TIME,
            timestamp=1,
            event_id="evt",
            delivery_id="dlv",
            body=b"{}",
        )
        rotated = self.platform.secrets.rotate(
            "ep-a", new_secret="new-secret", now=BASE_TIME, overlap_seconds=30
        )
        new_key, new_signature = self.platform.secrets.sign(
            "ep-a",
            now=BASE_TIME,
            timestamp=1,
            event_id="evt",
            delivery_id="dlv",
            body=b"{}",
        )
        self.assertEqual((old_key, new_key), ("v1", "v2"))
        self.assertNotEqual(old_signature, new_signature)
        self.assertTrue(
            self.platform.secrets.verify(
                "ep-a",
                now=BASE_TIME + timedelta(seconds=29),
                key_id=old_key,
                signature=old_signature,
                timestamp=1,
                event_id="evt",
                delivery_id="dlv",
                body=b"{}",
            )
        )
        self.assertFalse(
            self.platform.secrets.verify(
                "ep-a",
                now=BASE_TIME + timedelta(seconds=30),
                key_id=old_key,
                signature=old_signature,
                timestamp=1,
                event_id="evt",
                delivery_id="dlv",
                body=b"{}",
            )
        )

    def test_signature_binds_delivery_and_body(self) -> None:
        key_id, signature = self.platform.secrets.sign(
            "ep-a",
            now=BASE_TIME,
            timestamp=10,
            event_id="evt",
            delivery_id="dlv-1",
            body=b'{"a":1}',
        )
        self.assertFalse(
            self.platform.secrets.verify(
                "ep-a",
                now=BASE_TIME,
                key_id=key_id,
                signature=signature,
                timestamp=10,
                event_id="evt",
                delivery_id="dlv-2",
                body=b'{"a":1}',
            )
        )

    def test_zero_overlap_retires_old_key_at_rotation_instant(self) -> None:
        self.platform.secrets.rotate(
            "ep-a", new_secret="new-secret", now=BASE_TIME, overlap_seconds=0
        )
        keys = self.platform.secrets.verification_secrets("ep-a", now=BASE_TIME)
        self.assertEqual([key.key_id for key in keys], ["v2"])

    def test_rotation_rejects_blank_secret_and_negative_overlap(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.secrets.rotate(
                "ep-a", new_secret=" ", now=BASE_TIME, overlap_seconds=30
            )
        with self.assertRaises(ValidationError):
            self.platform.secrets.rotate(
                "ep-a", new_secret="new", now=BASE_TIME, overlap_seconds=-1
            )


class RateLimitTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bucket = PersistentTokenBucket(self.database)

    def test_reserve_consumes_tokens_and_reports_retry(self) -> None:
        self.bucket.configure(
            "org-a",
            rate_per_second=2,
            burst=2,
            max_in_flight=4,
            now=BASE_TIME,
        )
        self.assertTrue(self.bucket.reserve("org-a", now=BASE_TIME).allowed)
        self.assertTrue(self.bucket.reserve("org-a", now=BASE_TIME).allowed)
        denied = self.bucket.reserve("org-a", now=BASE_TIME)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after_seconds, 0.5)

    def test_tokens_refill_and_cap_at_burst(self) -> None:
        self.bucket.configure(
            "org-a",
            rate_per_second=1,
            burst=3,
            max_in_flight=4,
            now=BASE_TIME,
        )
        self.bucket.reserve("org-a", amount=3, now=BASE_TIME)
        later = self.bucket.reserve(
            "org-a", amount=2, now=BASE_TIME + timedelta(seconds=10)
        )
        self.assertTrue(later.allowed)
        self.assertEqual(later.remaining, 1)

    def test_backward_clock_does_not_mint_tokens(self) -> None:
        self.bucket.configure(
            "org-a",
            rate_per_second=1,
            burst=1,
            max_in_flight=1,
            now=BASE_TIME,
        )
        self.bucket.reserve("org-a", now=BASE_TIME)
        decision = self.bucket.reserve(
            "org-a", now=BASE_TIME - timedelta(seconds=30)
        )
        self.assertFalse(decision.allowed)

    def test_concurrent_reservations_cannot_overspend_burst(self) -> None:
        self.bucket.configure(
            "org-a",
            rate_per_second=0.001,
            burst=1,
            max_in_flight=1,
            now=BASE_TIME,
        )
        barrier = threading.Barrier(6)

        def reserve(_):
            barrier.wait()
            return PersistentTokenBucket(self.database).reserve(
                "org-a", now=BASE_TIME
            ).allowed

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(results.count(True), 1)

    def test_policy_validation_rejects_nonfinite_and_invalid_limits(self) -> None:
        for kwargs in (
            {"rate_per_second": float("nan"), "burst": 2, "max_in_flight": 1},
            {"rate_per_second": 1, "burst": 0, "max_in_flight": 1},
            {"rate_per_second": 1, "burst": 2, "max_in_flight": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.bucket.configure("org-a", now=BASE_TIME, **kwargs)


if __name__ == "__main__":
    unittest.main()

