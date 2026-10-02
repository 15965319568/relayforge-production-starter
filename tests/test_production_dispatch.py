from __future__ import annotations

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from _production_support import ProductionCase
from _support import BASE_TIME, ScriptedTransport, query_one
from relayforge.config import ServiceConfig
from relayforge.errors import LeaseLost, TransportError
from relayforge.models import TransportResponse
from relayforge.production_models import DispatchOutcome
from relayforge.retry import RetryPolicy
from relayforge.timeutil import to_iso


class DispatchAdmissionTests(ProductionCase, unittest.TestCase):
    def test_admission_is_idempotent_for_same_live_lease(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        first = self.platform.dispatch.admit(
            delivery_id, claim.lease_token, now=BASE_TIME
        )
        second = self.platform.dispatch.admit(
            delivery_id, claim.lease_token, now=BASE_TIME
        )
        self.assertTrue(first.allowed)
        self.assertEqual(first, second)
        state = query_one(
            self.path,
            "SELECT tokens FROM tenant_bucket_state WHERE tenant_id = 'org-a'",
        )
        self.assertEqual(19.0, state["tokens"])

    def test_rate_denial_defers_without_attempt(self) -> None:
        self.platform.rate_limits.configure(
            "org-a",
            rate_per_second=1,
            burst=1,
            max_in_flight=10,
            now=BASE_TIME,
        )
        with self.database.write() as connection:
            connection.execute(
                "UPDATE tenant_bucket_state SET tokens = 0 WHERE tenant_id = 'org-a'"
            )
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        decision = self.platform.dispatch.admit(
            delivery_id, claim.lease_token, now=BASE_TIME
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("rate_limited", decision.reason)
        self.assertEqual(
            0,
            query_one(
                self.path,
                "SELECT COUNT(*) AS n FROM attempts WHERE delivery_id = ?",
                (delivery_id,),
            )["n"],
        )
        self.assertEqual("retry_wait", self.store.inspect_delivery(delivery_id)["state"])

    def test_open_circuit_defers_without_consuming_token(self) -> None:
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE endpoint_circuits SET state = 'open', opened_until = ?
                WHERE endpoint_id = 'ep-a'
                """,
                (to_iso(BASE_TIME + timedelta(minutes=1)),),
            )
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        decision = self.platform.dispatch.admit(
            delivery_id, claim.lease_token, now=BASE_TIME
        )
        self.assertEqual("circuit_open", decision.reason)
        tokens = query_one(
            self.path,
            "SELECT tokens FROM tenant_bucket_state WHERE tenant_id = 'org-a'",
        )["tokens"]
        self.assertEqual(20.0, tokens)

    def test_concurrent_admissions_cannot_overspend_one_token(self) -> None:
        self.platform.rate_limits.configure(
            "org-a", rate_per_second=0.01, burst=1, max_in_flight=10, now=BASE_TIME
        )
        first_id = self.enqueue(suffix="one")
        second_id = self.enqueue(suffix="two")
        claims = [self.claim(first_id), self.claim(second_id)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(
                pool.map(
                    lambda item: self.platform.dispatch.admit(
                        item.delivery_id, item.lease_token, now=BASE_TIME
                    ),
                    claims,
                )
            )
        self.assertEqual(1, sum(item.allowed for item in decisions))
        self.assertEqual(1, sum(item.reason == "rate_limited" for item in decisions))

    def test_stale_lease_cannot_reuse_admission(self) -> None:
        delivery_id = self.enqueue()
        first = self.claim(delivery_id)
        self.platform.dispatch.admit(delivery_id, first.lease_token, now=BASE_TIME)
        self.clock.advance(seconds=31)
        second = self.claim(delivery_id)
        with self.assertRaises(LeaseLost):
            self.platform.dispatch.start_attempt(
                delivery_id, first.lease_token, now=self.clock()
            )
        decision = self.platform.dispatch.admit(
            delivery_id, second.lease_token, now=self.clock()
        )
        self.assertTrue(decision.allowed)


class DeliveryProcessorTests(ProductionCase, unittest.TestCase):
    def test_success_uses_current_v2_secret_and_commits_admission(self) -> None:
        self.platform.secrets.rotate(
            "ep-a", new_secret="next-secret", now=BASE_TIME, overlap_seconds=60
        )
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        transport = ScriptedTransport(TransportResponse(204, {"X-Request-Id": "rq-1"}))
        result = self.processor(transport).process(claim)
        self.assertEqual(DispatchOutcome.SUCCEEDED, result.outcome)
        request = transport.requests[0]
        self.assertEqual("v2", request.headers["X-Relay-Key-Id"])
        self.assertTrue(request.headers["X-Relay-Signature"].startswith("v2="))
        admission = query_one(
            self.path,
            "SELECT * FROM dispatch_admissions WHERE delivery_id = ?",
            (delivery_id,),
        )
        self.assertEqual("committed", admission["state"])

    def test_retryable_response_updates_retry_and_circuit_in_one_commit(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        transport = ScriptedTransport(TransportResponse(503, {"Retry-After": "1"}, b"busy"))
        result = self.processor(transport).process(claim)
        self.assertEqual(DispatchOutcome.RETRIED, result.outcome)
        delivery = self.store.inspect_delivery(delivery_id)
        self.assertEqual("retry_wait", delivery["state"])
        circuit = query_one(
            self.path,
            "SELECT * FROM endpoint_circuits WHERE endpoint_id = 'ep-a'",
        )
        self.assertEqual(1, circuit["failure_count"])

    def test_terminal_4xx_does_not_count_as_connectivity_failure(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        result = self.processor(ScriptedTransport(TransportResponse(422, {}, b"bad"))).process(claim)
        self.assertEqual(DispatchOutcome.DEAD, result.outcome)
        circuit = query_one(
            self.path,
            "SELECT * FROM endpoint_circuits WHERE endpoint_id = 'ep-a'",
        )
        self.assertEqual(("closed", 0), (circuit["state"], circuit["failure_count"]))

    def test_transport_error_text_never_reaches_persistence(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        result = self.processor(
            ScriptedTransport(TransportError("password=do-not-store"))
        ).process(claim)
        self.assertEqual(DispatchOutcome.RETRIED, result.outcome)
        attempt = query_one(
            self.path,
            "SELECT * FROM attempts WHERE delivery_id = ?",
            (delivery_id,),
        )
        self.assertEqual("transport:TransportError", attempt["error_kind"])
        self.assertNotIn("do-not-store", str(dict(attempt)))

    def test_expired_lease_fences_before_transport(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        self.clock.advance(seconds=31)
        transport = ScriptedTransport(TransportResponse(200, {}))
        result = self.processor(transport).process(claim)
        self.assertEqual(DispatchOutcome.LEASE_LOST, result.outcome)
        self.assertEqual([], transport.requests)

    def test_attempt_exhaustion_creates_dead_letter(self) -> None:
        config = ServiceConfig(retry=RetryPolicy(max_attempts=1))
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        processor = self.processor(ScriptedTransport(TransportResponse(500, {})))
        processor.config = config
        result = processor.process(claim)
        self.assertEqual(DispatchOutcome.DEAD, result.outcome)
        self.assertEqual(
            delivery_id,
            query_one(
                self.path,
                "SELECT delivery_id FROM dead_letters WHERE delivery_id = ?",
                (delivery_id,),
            )["delivery_id"],
        )
