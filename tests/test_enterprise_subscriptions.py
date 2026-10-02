from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from _enterprise_support import EnterpriseCase
from _support import BASE_TIME, ROOT
from relayforge.control_db import ControlDatabase
from relayforge.errors import IdempotencyConflict, ValidationError
from relayforge.store import Store
from relayforge.subscriptions import SubscriptionRepository


class ControlDatabaseTests(EnterpriseCase, unittest.TestCase):
    def test_initialize_backfills_control_rows_for_existing_endpoints(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "subscriptions",
                    "endpoint_secret_versions",
                    "endpoint_circuits",
                    "tenant_rate_policies",
                )
            )
        self.assertEqual(counts, (3, 3, 3, 2))

    def test_initialize_is_idempotent(self) -> None:
        self.database.initialize(now=BASE_TIME + timedelta(hours=1))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
                3,
            )

    def test_unknown_control_version_is_rejected_without_mutation(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE control_meta SET version = 99")
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "unsupported control schema version"):
            self.database.initialize(now=BASE_TIME)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(
                connection.execute("SELECT version FROM control_meta").fetchone()[0],
                99,
            )

    def test_control_v1_upgrade_preserves_subscription_ids(self) -> None:
        import json

        with TemporaryDirectory() as temp:
            path = str(Path(temp) / "control-v1.sqlite3")
            store = Store(path)
            store.initialize()
            endpoints = json.loads(
                (ROOT / "fixtures" / "endpoints.json").read_text(encoding="utf-8")
            )
            for endpoint in endpoints:
                values = dict(endpoint)
                values["endpoint_id"] = values.pop("id")
                store.register_endpoint(**values, now=BASE_TIME)
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    (ROOT / "fixtures" / "control_v1.sql").read_text(encoding="utf-8")
                )
            ControlDatabase(path).initialize(now=BASE_TIME)
            with closing(sqlite3.connect(path)) as connection:
                version = connection.execute(
                    "SELECT version FROM control_meta"
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT id, filter_json FROM subscriptions WHERE id = ?",
                    ("sub_01J9KX17TB3CFV77VX96M3B8Q2",),
                ).fetchone()
                legacy = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'legacy_control_%'"
                ).fetchone()[0]
            self.assertEqual(version, 2)
            self.assertEqual(row, ("sub_01J9KX17TB3CFV77VX96M3B8Q2", "{}"))
            self.assertEqual(legacy, 0)


class SubscriptionTests(EnterpriseCase, unittest.TestCase):
    def disable_defaults(self) -> None:
        for subscription in self.platform.subscriptions.list_for_tenant("org-a"):
            self.platform.subscriptions.set_enabled(
                subscription.id, enabled=False, now=BASE_TIME
            )

    def test_default_subscriptions_preserve_v1_fanout(self) -> None:
        self.assertEqual(
            self.platform.subscriptions.route(
                tenant_id="org-a", topic="anything.here", payload={}
            ),
            ["ep-a", "ep-b"],
        )

    def test_pattern_and_filter_route_only_matching_endpoint(self) -> None:
        self.disable_defaults()
        self.platform.subscriptions.create(
            tenant_id="org-a",
            endpoint_id="ep-a",
            topic_pattern="payment.*",
            filter_expression={"amount": {"$gte": 100}, "currency": "CNY"},
            now=BASE_TIME,
        )
        self.platform.subscriptions.create(
            tenant_id="org-a",
            endpoint_id="ep-b",
            topic_pattern="refund.#",
            filter_expression={},
            now=BASE_TIME,
        )
        self.assertEqual(
            self.platform.subscriptions.route(
                tenant_id="org-a",
                topic="payment.captured",
                payload={"amount": 120, "currency": "CNY"},
            ),
            ["ep-a"],
        )
        self.assertEqual(
            self.platform.subscriptions.route(
                tenant_id="org-a", topic="refund.created.manual", payload={}
            ),
            ["ep-b"],
        )

    def test_multiple_matching_subscriptions_deduplicate_endpoint(self) -> None:
        self.disable_defaults()
        for pattern in ("payment.*", "payment.#"):
            self.platform.subscriptions.create(
                tenant_id="org-a",
                endpoint_id="ep-a",
                topic_pattern=pattern,
                filter_expression={},
                now=BASE_TIME,
            )
        self.assertEqual(
            self.platform.subscriptions.route(
                tenant_id="org-a", topic="payment.captured", payload={}
            ),
            ["ep-a"],
        )

    def test_subscription_cannot_cross_tenant_boundary(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.subscriptions.create(
                tenant_id="org-a",
                endpoint_id="ep-c",
                topic_pattern="#",
                filter_expression={},
                now=BASE_TIME,
            )

    def test_disabled_subscription_no_longer_routes(self) -> None:
        subscription = self.platform.subscriptions.list_for_tenant("org-a")[0]
        self.platform.subscriptions.set_enabled(
            subscription.id, enabled=False, now=BASE_TIME
        )
        routed = self.platform.subscriptions.route(
            tenant_id="org-a", topic="test.event", payload={}
        )
        self.assertNotIn(subscription.endpoint_id, routed)

    def test_routed_ingest_snapshots_matches_and_is_idempotent(self) -> None:
        self.disable_defaults()
        self.platform.subscriptions.create(
            tenant_id="org-a",
            endpoint_id="ep-a",
            topic_pattern="payment.*",
            filter_expression={"currency": "CNY"},
            now=BASE_TIME,
        )
        first = self.platform.ingest_routed_event(
            tenant_id="org-a",
            topic="payment.captured",
            payload={"currency": "CNY", "amount": 20},
            idempotency_key="payment-1",
            event_id="evt-routed",
            now=BASE_TIME,
        )
        second = self.platform.ingest_routed_event(
            tenant_id="org-a",
            topic="payment.captured",
            payload={"amount": 20, "currency": "CNY"},
            idempotency_key="payment-1",
            now=BASE_TIME + timedelta(minutes=1),
        )
        self.assertEqual(first, ("evt-routed", True, ("ep-a",)))
        self.assertEqual(second, ("evt-routed", False, ("ep-a",)))

    def test_routed_ingest_rejects_idempotency_content_change(self) -> None:
        self.platform.ingest_routed_event(
            tenant_id="org-a",
            topic="payment.captured",
            payload={"amount": 20},
            idempotency_key="payment-2",
            now=BASE_TIME,
        )
        with self.assertRaises(IdempotencyConflict):
            self.platform.ingest_routed_event(
                tenant_id="org-a",
                topic="payment.failed",
                payload={"amount": 20},
                idempotency_key="payment-2",
                now=BASE_TIME,
            )


if __name__ == "__main__":
    unittest.main()
