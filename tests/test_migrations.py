from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from _support import ROOT, query_one
from relayforge.store import Store
from relayforge.timeutil import from_iso


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "legacy.sqlite3")
        sql = (ROOT / "fixtures" / "legacy_v0.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(sql)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migration_reaches_v1_and_removes_legacy_tables(self) -> None:
        Store(self.path).initialize()
        self.assertEqual(
            query_one(self.path, "SELECT version FROM schema_meta")["version"], 1
        )
        with closing(sqlite3.connect(self.path)) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertFalse(any(name.startswith("legacy_") for name in names))

    def test_states_and_unfinished_attempt_are_mapped_without_history_loss(self) -> None:
        Store(self.path).initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            states = dict(connection.execute("SELECT id, state FROM deliveries"))
            attempts = connection.execute(
                "SELECT COUNT(*), SUM(outcome = 'abandoned') FROM attempts"
            ).fetchone()
        self.assertEqual(
            set(states.values()), {"pending", "retry_wait", "succeeded", "dead"}
        )
        self.assertEqual(attempts, (7, 1))

    def test_timestamps_and_payloads_are_normalized(self) -> None:
        Store(self.path).initialize()
        endpoint = query_one(
            self.path,
            "SELECT created_at FROM endpoints WHERE id = ?",
            ("ep_01J8K4F3P8P8M7Q2KCPZ0V7M2A",),
        )
        event = query_one(
            self.path,
            "SELECT payload_json FROM events WHERE id = ?",
            ("evt_01J8QAQ2AFY84R1C0PS2NW3YH4",),
        )
        self.assertEqual(endpoint["created_at"], "2026-09-18T03:11:42Z")
        self.assertEqual(event["payload_json"], json.dumps(
            {
                "amount": 43800,
                "currency": "CNY",
                "merchant_ref": "SO-20260920-18473",
                "payment_id": "pay_8fa31c",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))

    def test_migration_and_reinitialization_preserve_foreign_key_integrity(self) -> None:
        store = Store(self.path)
        store.initialize()
        store.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            migration_rows = connection.execute(
                "SELECT COUNT(*) FROM audit_log WHERE kind = 'schema_migration'"
            ).fetchone()[0]
        self.assertEqual(violations, [])
        self.assertEqual(migration_rows, 1)

    def test_migrated_dead_letter_can_be_replayed_with_attempts_preserved(self) -> None:
        store = Store(self.path)
        store.initialize()
        delivery_id = "dlv_01J8QAR91NQ8KZP6D5FHM7B0VT"
        before = store.inspect_delivery(delivery_id)
        replayed = store.replay_dead_letters(
            now=from_iso("2026-09-20T09:00:00Z")
        )
        after = store.inspect_delivery(delivery_id)
        self.assertEqual(replayed, [delivery_id])
        self.assertEqual(after["state"], "pending")
        self.assertEqual(len(after["attempts"]), len(before["attempts"]))

    def test_invalid_legacy_status_rolls_back_renames(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE deliveries SET status = 'mystery' WHERE id = ?",
                ("dlv_01J8QAQ2G1G4X66QGZ0K8M5T4A",),
            )
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "unsupported legacy delivery status"):
            Store(self.path).initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            original = connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status = 'mystery'"
            ).fetchone()[0]
            legacy_tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'legacy_%'"
            ).fetchone()[0]
        self.assertEqual((version, original, legacy_tables), (0, 1, 0))


if __name__ == "__main__":
    unittest.main()
