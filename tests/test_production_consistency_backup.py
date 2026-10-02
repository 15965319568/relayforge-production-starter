from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from _production_support import ProductionCase
from _support import BASE_TIME, query_one
from relayforge.backup import BackupService, validate_database
from relayforge.errors import ValidationError
from relayforge.production_models import RepairPlanState


class ConsistencyAndRepairTests(ProductionCase, unittest.TestCase):
    def test_clean_database_has_deterministic_empty_fingerprint(self) -> None:
        first = self.platform.consistency.scan(now=BASE_TIME, run_id="scan-one")
        second = self.platform.consistency.scan(now=BASE_TIME, run_id="scan-two")
        self.assertEqual((), first.findings)
        with self.database.connection() as connection:
            fingerprints = [
                row[0]
                for row in connection.execute(
                    "SELECT fingerprint FROM consistency_runs ORDER BY id"
                )
            ]
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_scan_does_not_mutate_corrupt_business_rows(self) -> None:
        delivery_id = self.enqueue()
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE deliveries SET state = 'dead', completed_at = ?,
                    last_error = 'fixture-corruption' WHERE id = ?
                """,
                (BASE_TIME.isoformat(), delivery_id),
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        self.assertIn("DEAD_MISSING_LETTER", {item.code for item in report.findings})
        count = query_one(
            self.path,
            "SELECT COUNT(*) AS n FROM dead_letters WHERE delivery_id = ?",
            (delivery_id,),
        )["n"]
        self.assertEqual(0, count)

    def test_repair_plan_applies_dead_letter_and_lease_repairs_atomically(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE deliveries SET state = 'dead', completed_at = ?,
                    last_error = 'manual-corruption' WHERE id = ?
                """,
                (BASE_TIME.isoformat(), delivery_id),
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        plan = self.platform.repair_plans.create(report.run_id, now=BASE_TIME)
        self.assertEqual(2, plan.action_count)
        applied = self.platform.repairs.apply(plan.id, now=BASE_TIME)
        self.assertEqual(2, applied)
        repaired = query_one(
            self.path, "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
        )
        self.assertIsNone(repaired["lease_token"])
        self.assertEqual(
            delivery_id,
            query_one(
                self.path,
                "SELECT delivery_id FROM dead_letters WHERE delivery_id = ?",
                (delivery_id,),
            )["delivery_id"],
        )

    def test_plan_becomes_stale_when_source_entity_changes(self) -> None:
        delivery_id = self.enqueue()
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE deliveries SET state = 'dead', completed_at = ?,
                    last_error = 'fixture' WHERE id = ?
                """,
                (BASE_TIME.isoformat(), delivery_id),
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        plan = self.platform.repair_plans.create(report.run_id, now=BASE_TIME)
        with self.database.write() as connection:
            connection.execute(
                "UPDATE deliveries SET version = version + 1 WHERE id = ?",
                (delivery_id,),
            )
        self.assertEqual(0, self.platform.repairs.apply(plan.id, now=BASE_TIME))
        refreshed, _ = self.platform.repair_plans.get(plan.id)
        self.assertEqual(RepairPlanState.STALE, refreshed.state)

    def test_bucket_and_circuit_corruption_is_detected_and_repaired(self) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE tenant_bucket_state SET tokens = 999 WHERE tenant_id = 'org-a'"
            )
            connection.execute(
                """
                UPDATE endpoint_circuits SET state = 'half_open', probe_token = NULL
                WHERE endpoint_id = 'ep-a'
                """
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        self.assertEqual(
            {"BUCKET_OUT_OF_RANGE", "CIRCUIT_PROBE_MISMATCH"},
            {item.code for item in report.findings},
        )
        plan = self.platform.repair_plans.create(report.run_id, now=BASE_TIME)
        self.assertEqual(2, self.platform.repairs.apply(plan.id, now=BASE_TIME))
        self.assertEqual(
            20.0,
            query_one(
                self.path,
                "SELECT tokens FROM tenant_bucket_state WHERE tenant_id = 'org-a'",
            )["tokens"],
        )

    def test_replay_counter_mismatch_uses_item_truth(self) -> None:
        delivery_id = self.enqueue()
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO replay_jobs(
                    id, idempotency_key, request_hash, state,
                    total_count, processed_count, skipped_count,
                    created_at, updated_at
                ) VALUES ('job-x', 'key-x', 'hash-x', 'running', 9, 8, 7, ?, ?)
                """,
                (BASE_TIME.isoformat(), BASE_TIME.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO replay_job_items(job_id, delivery_id, ordinal, state)
                VALUES ('job-x', ?, 1, 'pending')
                """,
                (delivery_id,),
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        plan = self.platform.repair_plans.create(report.run_id, now=BASE_TIME)
        self.platform.repairs.apply(plan.id, now=BASE_TIME)
        row = query_one(self.path, "SELECT * FROM replay_jobs WHERE id = 'job-x'")
        self.assertEqual((1, 0, 0), (row["total_count"], row["processed_count"], row["skipped_count"]))

    def test_endpoint_with_no_current_secret_is_reported(self) -> None:
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM endpoint_secret_versions WHERE endpoint_id = 'ep-a'"
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        finding = next(item for item in report.findings if item.code == "SECRET_CURRENT_COUNT")
        self.assertEqual(("ep-a", 0), (finding.entity_id, finding.details["current_count"]))

    def test_multiple_versioned_repairs_for_one_delivery_are_sequenced(self) -> None:
        delivery_id = self.enqueue(suffix="multi-repair")
        claim = self.claim(delivery_id)
        self.store.start_attempt(delivery_id, claim.lease_token, now=BASE_TIME)
        with self.database.write() as connection:
            connection.execute(
                """
                UPDATE deliveries SET state = 'dead', attempt_count = 0,
                    completed_at = ?, last_error = 'fixture' WHERE id = ?
                """,
                (BASE_TIME.isoformat().replace("+00:00", "Z"), delivery_id),
            )
        report = self.platform.consistency.scan(now=BASE_TIME)
        plan = self.platform.repair_plans.create(report.run_id, now=BASE_TIME)
        self.assertEqual(3, self.platform.repairs.apply(plan.id, now=BASE_TIME))
        repaired = query_one(
            self.path, "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
        )
        self.assertEqual(1, repaired["attempt_count"])
        self.assertIsNone(repaired["lease_token"])


class BackupTests(ProductionCase, unittest.TestCase):
    def test_backup_has_checksum_manifest_and_passes_integrity_checks(self) -> None:
        self.enqueue()
        target = self.root / "backup.sqlite3"
        manifest = self.platform.backups.create(target, now=BASE_TIME)
        self.assertTrue(target.is_file())
        self.assertTrue(Path(str(target) + ".manifest.json").is_file())
        self.assertEqual((1, 2, 1), validate_database(target))
        sidecar = json.loads(Path(str(target) + ".manifest.json").read_text())
        self.assertEqual(manifest.sha256, sidecar["sha256"])
        self.assertNotIn("legacy-a", json.dumps(sidecar))

    def test_restore_refuses_existing_target(self) -> None:
        backup = self.root / "backup.sqlite3"
        self.platform.backups.create(backup, now=BASE_TIME)
        target = self.root / "existing.sqlite3"
        target.write_bytes(b"do not overwrite")
        with self.assertRaises(ValidationError):
            BackupService.restore(backup, target)
        self.assertEqual(b"do not overwrite", target.read_bytes())

    def test_restore_rejects_tampered_backup_and_leaves_no_target(self) -> None:
        backup = self.root / "backup.sqlite3"
        self.platform.backups.create(backup, now=BASE_TIME)
        with backup.open("ab") as handle:
            handle.write(b"tamper")
        target = self.root / "restored.sqlite3"
        with self.assertRaises(ValidationError):
            BackupService.restore(backup, target)
        self.assertFalse(target.exists())

    def test_restore_to_new_database_preserves_rows(self) -> None:
        delivery_id = self.enqueue()
        backup = self.root / "backup.sqlite3"
        self.platform.backups.create(backup, now=BASE_TIME)
        target = self.root / "restored.sqlite3"
        BackupService.restore(backup, target)
        connection = sqlite3.connect(target)
        try:
            row = connection.execute(
                "SELECT id FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(delivery_id, row[0])
