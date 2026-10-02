from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta

from _enterprise_support import EnterpriseCase
from _support import BASE_TIME
from relayforge.control_models import ReplayJobState
from relayforge.errors import IdempotencyConflict, InvalidTransition
from relayforge.replay_jobs import ReplayJobService
from relayforge.retention import RetentionService


class ReplayJobTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = ReplayJobService(self.database)

    def test_create_snapshots_matching_dead_letters(self) -> None:
        first = self.make_dead(suffix="1", reason="http:422")
        self.make_dead(suffix="2", reason="http:500")
        job, created = self.service.create(
            idempotency_key="job-1",
            tenant_id="org-a",
            reason="http:422",
            now=BASE_TIME + timedelta(minutes=1),
        )
        self.assertTrue(created)
        self.assertEqual(job.total_count, 1)
        with closing(sqlite3.connect(self.path)) as connection:
            item = connection.execute(
                "SELECT delivery_id FROM replay_job_items WHERE job_id = ?",
                (job.id,),
            ).fetchone()[0]
        self.assertEqual(item, first)

    def test_create_is_idempotent_but_detects_request_mismatch(self) -> None:
        self.make_dead(suffix="1")
        first, created = self.service.create(
            idempotency_key="same", tenant_id="org-a", now=BASE_TIME
        )
        second, second_created = self.service.create(
            idempotency_key="same", tenant_id="org-a", now=BASE_TIME
        )
        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        with self.assertRaises(IdempotencyConflict):
            self.service.create(
                idempotency_key="same", tenant_id="org-b", now=BASE_TIME
            )

    def test_run_batches_resume_to_completion(self) -> None:
        ids = [self.make_dead(suffix=str(index)) for index in range(3)]
        job, _ = self.service.create(idempotency_key="batch", now=BASE_TIME)
        first = self.service.run_batch(job.id, now=BASE_TIME, limit=2)
        second = self.service.run_batch(job.id, now=BASE_TIME, limit=2)
        self.assertEqual((first.applied, first.remaining), (2, 1))
        self.assertEqual((second.applied, second.remaining), (1, 0))
        self.assertEqual(second.state, ReplayJobState.COMPLETED)
        for delivery_id in ids:
            self.assertEqual(self.store.inspect_delivery(delivery_id)["state"], "pending")

    def test_snapshot_does_not_absorb_later_dead_letters(self) -> None:
        first = self.make_dead(suffix="1")
        job, _ = self.service.create(idempotency_key="snapshot", now=BASE_TIME)
        later = self.make_dead(suffix="2", at=BASE_TIME + timedelta(minutes=2))
        result = self.service.run_batch(job.id, now=BASE_TIME + timedelta(minutes=3))
        self.assertEqual(result.applied, 1)
        self.assertEqual(self.store.inspect_delivery(first)["state"], "pending")
        self.assertEqual(self.store.inspect_delivery(later)["state"], "dead")

    def test_already_replayed_item_is_skipped_not_double_counted(self) -> None:
        delivery = self.make_dead(suffix="1")
        job, _ = self.service.create(idempotency_key="skip", now=BASE_TIME)
        self.store.replay_dead_letters(now=BASE_TIME + timedelta(minutes=1))
        result = self.service.run_batch(job.id, now=BASE_TIME + timedelta(minutes=2))
        self.assertEqual((result.applied, result.skipped), (0, 1))
        letter = self.store.list_dead_letters()
        self.assertEqual(letter, [])

    def test_cancelled_job_cannot_run(self) -> None:
        self.make_dead(suffix="1")
        job, _ = self.service.create(idempotency_key="cancel", now=BASE_TIME)
        self.service.cancel(job.id, now=BASE_TIME)
        with self.assertRaises(InvalidTransition):
            self.service.run_batch(job.id, now=BASE_TIME)


class RetentionTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.retention = RetentionService(self.database)

    def test_plan_and_dry_run_do_not_mutate(self) -> None:
        delivery = self.make_succeeded(at=BASE_TIME)
        self.store.append_audit(
            occurred_at=BASE_TIME,
            kind="request",
            delivery_id=delivery,
            details={"safe": True},
        )
        cutoff = BASE_TIME + timedelta(hours=1)
        plan = self.retention.plan(response_before=cutoff, audit_before=cutoff)
        result = self.retention.apply(
            response_before=cutoff,
            audit_before=cutoff,
            now=cutoff,
            dry_run=True,
        )
        self.assertEqual(plan.response_excerpts, 1)
        self.assertEqual(result.response_excerpts_cleared, 1)
        self.assertIsNotNone(
            self.store.inspect_delivery(delivery)["attempts"][0]["response_excerpt"]
        )

    def test_apply_clears_succeeded_excerpt_and_old_audit(self) -> None:
        delivery = self.make_succeeded(at=BASE_TIME)
        self.store.append_audit(
            occurred_at=BASE_TIME,
            kind="request",
            delivery_id=delivery,
            details={},
        )
        cutoff = BASE_TIME + timedelta(hours=1)
        result = self.retention.apply(
            response_before=cutoff,
            audit_before=cutoff,
            now=cutoff,
        )
        self.assertEqual((result.response_excerpts_cleared, result.audit_rows_deleted), (1, 1))
        self.assertIsNone(
            self.store.inspect_delivery(delivery)["attempts"][0]["response_excerpt"]
        )

    def test_dead_and_active_history_is_never_removed(self) -> None:
        dead = self.make_dead(suffix="dead", at=BASE_TIME)
        self.store.append_audit(
            occurred_at=BASE_TIME,
            kind="response",
            delivery_id=dead,
            details={},
        )
        cutoff = BASE_TIME + timedelta(days=10)
        result = self.retention.apply(
            response_before=cutoff,
            audit_before=cutoff,
            now=cutoff,
        )
        self.assertEqual((result.response_excerpts_cleared, result.audit_rows_deleted), (0, 0))
        self.assertIsNotNone(
            self.store.inspect_delivery(dead)["attempts"][0]["response_excerpt"]
        )

    def test_schema_migration_audit_is_retained(self) -> None:
        self.store.append_audit(
            occurred_at=BASE_TIME,
            kind="schema_migration",
            delivery_id=None,
            details={"from": 0, "to": 1},
        )
        cutoff = BASE_TIME + timedelta(days=1)
        self.retention.apply(
            response_before=cutoff,
            audit_before=cutoff,
            now=cutoff,
        )
        with self.database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM audit_log WHERE kind = 'schema_migration'"
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

