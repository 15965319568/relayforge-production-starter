from __future__ import annotations

import json
import unittest
from datetime import timedelta

from _support import (
    BASE_TIME,
    FakeClock,
    ScriptedTransport,
    StoreFixture,
    query_one,
    seed_delivery,
)
from relayforge.config import ServiceConfig
from relayforge.engine import DeliveryEngine
from relayforge.errors import TransportError
from relayforge.models import TransportResponse
from relayforge.retry import RetryPolicy
from relayforge.timeutil import from_iso


class DeliveryEngineTests(StoreFixture, unittest.TestCase):
    def build_engine(
        self,
        transport: ScriptedTransport,
        *,
        clock: FakeClock | None = None,
        retry: RetryPolicy | None = None,
        lease_seconds: int = 30,
    ) -> DeliveryEngine:
        return DeliveryEngine(
            store=self.store,
            transport=transport,
            config=ServiceConfig(
                lease_seconds=lease_seconds,
                retry=retry or RetryPolicy(jitter_ratio=0),
            ),
            clock=clock or FakeClock(),
        )

    def test_success_sends_canonical_signed_request_and_finalizes(self) -> None:
        delivery_id = seed_delivery(
            self.store, payload={"z": 2, "a": "你好"}, secret="top-secret"
        )
        transport = ScriptedTransport(
            TransportResponse(204, {"X-Request-Id": "req-1"}, b"done")
        )
        summary = self.build_engine(transport).run_once(worker_id="w-1")
        self.assertEqual(summary.as_dict()["succeeded"], 1)
        request = transport.requests[0]
        self.assertEqual(request.body, '{"a":"你好","z":2}'.encode("utf-8"))
        self.assertEqual(request.headers["Idempotency-Key"], delivery_id)
        self.assertTrue(request.headers["X-Relay-Signature"].startswith("v1="))
        inspected = self.store.inspect_delivery(delivery_id)
        self.assertEqual(inspected["state"], "succeeded")
        self.assertEqual(inspected["attempts"][0]["request_id"], "req-1")

    def test_retryable_status_moves_delivery_to_retry_wait(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(TransportResponse(503, {}, b"unavailable"))
        summary = self.build_engine(transport).run_once(worker_id="w-1")
        self.assertEqual(summary.retried, 1)
        inspected = self.store.inspect_delivery(delivery_id)
        self.assertEqual(inspected["state"], "retry_wait")
        self.assertEqual(inspected["last_status"], 503)
        self.assertEqual(inspected["attempts"][0]["outcome"], "retryable")

    def test_retry_after_is_case_insensitive_and_a_lower_bound(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(
            TransportResponse(429, {"rEtRy-AfTeR": "120"}, b"slow down")
        )
        self.build_engine(transport).run_once(worker_id="w-1")
        inspected = self.store.inspect_delivery(delivery_id)
        self.assertEqual(
            from_iso(inspected["next_attempt_at"]), BASE_TIME + timedelta(seconds=120)
        )

    def test_terminal_status_moves_delivery_to_dead_letter(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(TransportResponse(422, {}, b"invalid"))
        summary = self.build_engine(transport).run_once(worker_id="w-1")
        self.assertEqual(summary.dead, 1)
        self.assertEqual(self.store.inspect_delivery(delivery_id)["state"], "dead")
        letters = self.store.list_dead_letters()
        self.assertEqual(letters[0]["reason"], "http:422")

    def test_transport_error_is_retryable_without_logging_exception_message(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(TransportError("password=do-not-log"))
        summary = self.build_engine(transport).run_once(worker_id="w-1")
        self.assertEqual(summary.retried, 1)
        audit_text = json.dumps(self.store.list_audit(delivery_id))
        self.assertNotIn("do-not-log", audit_text)

    def test_max_attempts_one_dead_letters_first_retryable_failure(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(TransportResponse(500, {}, b"broken"))
        retry = RetryPolicy(max_attempts=1, jitter_ratio=0)
        summary = self.build_engine(transport, retry=retry).run_once(worker_id="w-1")
        self.assertEqual(summary.dead, 1)
        self.assertEqual(
            self.store.list_dead_letters()[0]["reason"], "attempts_exhausted"
        )

    def test_retry_beyond_elapsed_window_is_dead_lettered(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(TransportResponse(503, {}, b"broken"))
        retry = RetryPolicy(
            base_seconds=10, max_elapsed_seconds=9, jitter_ratio=0, max_attempts=4
        )
        summary = self.build_engine(transport, retry=retry).run_once(worker_id="w-1")
        self.assertEqual(summary.dead, 1)
        self.assertEqual(
            self.store.list_dead_letters()[0]["reason"], "retry_window_exhausted"
        )

    def test_audit_redacts_signature_and_endpoint_query(self) -> None:
        delivery_id = seed_delivery(
            self.store,
            url="https://user:pass@example.test/hook?api_key=query-secret#frag",
            secret="signing-secret",
        )
        transport = ScriptedTransport(TransportResponse(200, {}, b"ok"))
        self.build_engine(transport).run_once(worker_id="w-1")
        request_audit = self.store.list_audit(delivery_id)[0]["details"]
        self.assertEqual(request_audit["url"], "https://example.test/hook")
        self.assertEqual(
            request_audit["headers"]["X-Relay-Signature"], "[REDACTED]"
        )
        audit_text = json.dumps(request_audit)
        self.assertNotIn("query-secret", audit_text)
        self.assertNotIn("signing-secret", audit_text)

    def test_response_excerpt_is_bounded(self) -> None:
        delivery_id = seed_delivery(self.store)
        transport = ScriptedTransport(TransportResponse(400, {}, b"x" * 400))
        self.build_engine(transport).run_once(worker_id="w-1")
        attempt = self.store.inspect_delivery(delivery_id)["attempts"][0]
        self.assertEqual(len(attempt["response_excerpt"]), 256)

    def test_metrics_and_summary_use_same_outcome_counts(self) -> None:
        seed_delivery(self.store)
        transport = ScriptedTransport(TransportResponse(200, {}, b"ok"))
        engine = self.build_engine(transport)
        summary = engine.run_once(worker_id="w-1")
        metrics = engine.metrics.snapshot()
        self.assertEqual(metrics["claimed"], summary.claimed)
        self.assertEqual(metrics["succeeded"], summary.succeeded)


if __name__ == "__main__":
    unittest.main()

