from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta

from _support import BASE_TIME, StoreFixture, first_delivery_id, query_one, seed_delivery
from relayforge.errors import IdempotencyConflict, LeaseLost, ValidationError


class StoreTests(StoreFixture, unittest.TestCase):
    def test_initialize_is_idempotent(self) -> None:
        self.store.initialize()
        row = query_one(self.db_path, "SELECT COUNT(*) AS n FROM schema_meta")
        self.assertEqual(row["n"], 1)

    def test_endpoint_requires_absolute_http_url(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.register_endpoint(
                tenant_id="a", url="/relative", secret="s", now=BASE_TIME
            )

    def test_enqueue_fans_out_to_current_enabled_endpoints(self) -> None:
        for endpoint in ("ep-1", "ep-2"):
            self.store.register_endpoint(
                tenant_id="a",
                url=f"https://{endpoint}.test/hook",
                secret="s",
                endpoint_id=endpoint,
                now=BASE_TIME,
            )
        event, created = self.store.enqueue_event(
            tenant_id="a",
            topic="thing.created",
            payload={"id": 1},
            idempotency_key="one",
            event_id="evt-1",
            now=BASE_TIME,
        )
        self.assertTrue(created)
        self.assertEqual(event.id, "evt-1")
        row = query_one(
            self.db_path,
            "SELECT COUNT(*) AS n FROM deliveries WHERE event_id = 'evt-1'",
        )
        self.assertEqual(row["n"], 2)

    def test_same_idempotency_key_and_semantic_json_returns_original(self) -> None:
        first, first_created = self.store.enqueue_event(
            tenant_id="a",
            topic="thing.created",
            payload={"b": 2, "a": 1},
            idempotency_key="one",
            event_id="evt-1",
            now=BASE_TIME,
        )
        second, second_created = self.store.enqueue_event(
            tenant_id="a",
            topic="thing.created",
            payload={"a": 1, "b": 2},
            idempotency_key="one",
            event_id="ignored",
            now=BASE_TIME + timedelta(hours=1),
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second.id, first.id)

    def test_idempotency_key_content_mismatch_is_a_conflict(self) -> None:
        self.store.enqueue_event(
            tenant_id="a",
            topic="thing.created",
            payload={"id": 1},
            idempotency_key="one",
            now=BASE_TIME,
        )
        with self.assertRaises(IdempotencyConflict):
            self.store.enqueue_event(
                tenant_id="a",
                topic="thing.created",
                payload={"id": 2},
                idempotency_key="one",
                now=BASE_TIME,
            )

    def test_endpoint_added_later_does_not_change_existing_fanout(self) -> None:
        self.store.enqueue_event(
            tenant_id="a",
            topic="thing.created",
            payload={},
            idempotency_key="one",
            event_id="evt-1",
            now=BASE_TIME,
        )
        self.store.register_endpoint(
            tenant_id="a",
            url="https://later.test/hook",
            secret="s",
            endpoint_id="later",
            now=BASE_TIME,
        )
        row = query_one(
            self.db_path,
            "SELECT COUNT(*) AS n FROM deliveries WHERE event_id = 'evt-1'",
        )
        self.assertEqual(row["n"], 0)

    def test_claim_sets_fencing_token_and_start_is_idempotent(self) -> None:
        delivery_id = seed_delivery(self.store)
        claims = self.store.claim_selected(
            [delivery_id], worker_id="worker-1", now=BASE_TIME, lease_seconds=30
        )
        self.assertEqual(len(claims), 1)
        ordinal_1 = self.store.start_attempt(
            delivery_id, claims[0].lease_token, now=BASE_TIME
        )
        ordinal_2 = self.store.start_attempt(
            delivery_id, claims[0].lease_token, now=BASE_TIME
        )
        self.assertEqual((ordinal_1, ordinal_2), (1, 1))

    def test_active_claim_cannot_be_claimed_again(self) -> None:
        delivery_id = seed_delivery(self.store)
        first = self.store.claim_selected(
            [delivery_id], worker_id="worker-1", now=BASE_TIME, lease_seconds=30
        )
        second = self.store.claim_selected(
            [delivery_id], worker_id="worker-2", now=BASE_TIME, lease_seconds=30
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_expired_claim_abandons_unfinished_attempt(self) -> None:
        delivery_id = seed_delivery(self.store)
        first = self.store.claim_selected(
            [delivery_id], worker_id="worker-1", now=BASE_TIME, lease_seconds=10
        )[0]
        self.store.start_attempt(delivery_id, first.lease_token, now=BASE_TIME)
        reclaimed = self.store.claim_selected(
            [delivery_id],
            worker_id="worker-2",
            now=BASE_TIME + timedelta(seconds=10),
            lease_seconds=20,
        )[0]
        self.assertNotEqual(first.lease_token, reclaimed.lease_token)
        attempt = query_one(
            self.db_path,
            "SELECT outcome, error_kind FROM attempts WHERE delivery_id = ?",
            (delivery_id,),
        )
        self.assertEqual(attempt["outcome"], "abandoned")
        self.assertEqual(attempt["error_kind"], "lease_expired")

    def test_old_token_is_fenced_after_reclaim(self) -> None:
        delivery_id = seed_delivery(self.store)
        first = self.store.claim_selected(
            [delivery_id], worker_id="worker-1", now=BASE_TIME, lease_seconds=10
        )[0]
        self.store.start_attempt(delivery_id, first.lease_token, now=BASE_TIME)
        self.store.claim_selected(
            [delivery_id],
            worker_id="worker-2",
            now=BASE_TIME + timedelta(seconds=10),
            lease_seconds=20,
        )
        with self.assertRaises(LeaseLost):
            self.store.finalize_success(
                delivery_id,
                first.lease_token,
                now=BASE_TIME + timedelta(seconds=11),
                status_code=200,
                request_id=None,
                response_body=b"ok",
            )

    def test_token_is_stale_at_exact_expiry_instant(self) -> None:
        delivery_id = seed_delivery(self.store)
        claim = self.store.claim_selected(
            [delivery_id], worker_id="worker", now=BASE_TIME, lease_seconds=10
        )[0]
        self.store.start_attempt(delivery_id, claim.lease_token, now=BASE_TIME)
        with self.assertRaises(LeaseLost):
            self.store.renew_lease(
                delivery_id,
                claim.lease_token,
                now=BASE_TIME + timedelta(seconds=10),
                lease_seconds=10,
            )

    def test_dead_letter_dry_run_does_not_mutate(self) -> None:
        delivery_id = seed_delivery(self.store)
        claim = self.store.claim_selected(
            [delivery_id], worker_id="worker", now=BASE_TIME, lease_seconds=30
        )[0]
        self.store.start_attempt(delivery_id, claim.lease_token, now=BASE_TIME)
        self.store.finalize_dead(
            delivery_id,
            claim.lease_token,
            now=BASE_TIME,
            reason="http:400",
            outcome="terminal",
            status_code=400,
            retry_after=None,
            request_id=None,
            response_body=b"bad",
        )
        ids = self.store.replay_dead_letters(now=BASE_TIME, dry_run=True)
        self.assertEqual(ids, [delivery_id])
        self.assertEqual(self.store.inspect_delivery(delivery_id)["state"], "dead")

    def test_replay_preserves_attempts_and_increments_audit_counter(self) -> None:
        delivery_id = seed_delivery(self.store)
        claim = self.store.claim_selected(
            [delivery_id], worker_id="worker", now=BASE_TIME, lease_seconds=30
        )[0]
        self.store.start_attempt(delivery_id, claim.lease_token, now=BASE_TIME)
        self.store.finalize_dead(
            delivery_id,
            claim.lease_token,
            now=BASE_TIME,
            reason="http:400",
            outcome="terminal",
            status_code=400,
            retry_after=None,
            request_id=None,
            response_body=b"bad",
        )
        self.store.replay_dead_letters(
            now=BASE_TIME + timedelta(minutes=1), limit=10
        )
        inspected = self.store.inspect_delivery(delivery_id)
        self.assertEqual(inspected["state"], "pending")
        self.assertEqual(len(inspected["attempts"]), 1)
        row = query_one(
            self.db_path,
            "SELECT replay_count FROM dead_letters WHERE delivery_id = ?",
            (delivery_id,),
        )
        self.assertEqual(row["replay_count"], 1)


if __name__ == "__main__":
    unittest.main()

