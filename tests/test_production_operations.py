from __future__ import annotations

import json
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest.mock import patch

from _production_support import ProductionCase
from _support import BASE_TIME, ScriptedTransport
from relayforge.models import TransportResponse
from relayforge.telemetry import BoundedMetrics, StructuredLogger, production_metrics
from relayforge.worker_runtime import WorkerRuntime
from relayforge.admin_cli import main as admin_main


class TelemetryTests(unittest.TestCase):
    def test_metric_labels_reject_entity_identifiers(self) -> None:
        metrics = BoundedMetrics()
        with self.assertRaises(ValueError):
            metrics.declare("bad_metric", labels=("tenant_id",))

    def test_metric_series_have_a_hard_cardinality_limit(self) -> None:
        metrics = BoundedMetrics(max_series=2)
        metrics.declare("requests", labels=("outcome",))
        metrics.increment("requests", labels={"outcome": "ok"})
        metrics.increment("requests", labels={"outcome": "failed"})
        with self.assertRaises(ValueError):
            metrics.increment("requests", labels={"outcome": "third"})

    def test_histogram_uses_fixed_cumulative_buckets(self) -> None:
        metrics = production_metrics()
        metrics.observe(
            "relayforge_dispatch_latency_seconds",
            0.2,
            buckets=(0.1, 0.5, 1.0),
            labels={"outcome": "succeeded"},
        )
        snapshot = metrics.histogram(
            "relayforge_dispatch_latency_seconds",
            labels={"outcome": "succeeded"},
        )
        self.assertEqual(((0.1, 0), (0.5, 1), (1.0, 1)), snapshot.buckets)

    def test_structured_logs_redact_secrets_payloads_headers_and_query(self) -> None:
        encoded = StructuredLogger().event(
            "dispatch",
            now=BASE_TIME,
            correlation_id="rq-1",
            fields={
                "token": "bearer-secret",
                "payload": {"card": "4111"},
                "target_url": "https://example.test/hook?key=query-secret",
                "headers": {"Authorization": "secret", "X-Test": "ok"},
            },
        )
        self.assertNotIn("bearer-secret", encoded)
        self.assertNotIn("4111", encoded)
        self.assertNotIn("query-secret", encoded)
        self.assertIn("[REDACTED]", encoded)

    def test_metrics_snapshot_is_json_serializable(self) -> None:
        metrics = production_metrics()
        metrics.observe(
            "relayforge_queue_age_seconds",
            2.5,
            buckets=(1, 5, 30),
            labels={"priority": "due"},
        )
        encoded = json.dumps(metrics.snapshot(), sort_keys=True)
        self.assertIn('"buckets"', encoded)


class HealthTests(ProductionCase, unittest.TestCase):
    def test_liveness_is_read_only_and_readiness_checks_write_path(self) -> None:
        live = self.platform.health.liveness()
        ready = self.platform.health.readiness()
        self.assertEqual("ok", live.status)
        self.assertEqual("ready", ready.status)
        self.assertEqual({"sqlite_read"}, set(live.checks))
        self.assertIn("sqlite_write_read", ready.checks)

    def test_stale_non_draining_worker_makes_service_not_ready(self) -> None:
        self.platform.workers.register("stale", now=BASE_TIME, metadata={})
        self.clock.advance(seconds=121)
        self.platform.health.clock = self.clock
        report = self.platform.health.readiness()
        self.assertEqual("not_ready", report.status)
        self.assertEqual(1, report.checks["workers"]["stale"])


class WorkerRuntimeTests(ProductionCase, unittest.TestCase):
    def test_runtime_processes_parallel_batch_and_heartbeats(self) -> None:
        self.enqueue(suffix="one")
        self.enqueue(suffix="two")
        transport = ScriptedTransport(
            TransportResponse(200, {}), TransportResponse(204, {})
        )
        runtime = WorkerRuntime(
            worker_id="worker-prod",
            store=self.store,
            registry=self.platform.workers,
            processor=self.processor(transport),
            concurrency=2,
            clock=self.clock,
        )
        summary = runtime.run_cycle(limit=2)
        self.assertEqual((2, 2), (summary.claimed, summary.succeeded))
        with self.database.connection() as connection:
            worker = connection.execute(
                "SELECT * FROM worker_nodes WHERE worker_id = 'worker-prod'"
            ).fetchone()
        self.assertIsNotNone(worker)

    def test_shutdown_stops_new_claims_and_marks_worker_draining(self) -> None:
        self.enqueue()
        runtime = WorkerRuntime(
            worker_id="worker-prod",
            store=self.store,
            registry=self.platform.workers,
            processor=self.processor(ScriptedTransport(TransportResponse(200, {}))),
            concurrency=1,
            clock=self.clock,
        )
        runtime.start()
        runtime.request_shutdown(timeout_seconds=10)
        summary = runtime.run_cycle()
        self.assertEqual(0, summary.claimed)
        with self.database.connection() as connection:
            draining = connection.execute(
                "SELECT draining FROM worker_nodes WHERE worker_id = 'worker-prod'"
            ).fetchone()[0]
        self.assertEqual(1, draining)

    def test_lease_supervisor_fences_stolen_lease(self) -> None:
        delivery_id = self.enqueue()
        claim = self.claim(delivery_id)
        runtime = WorkerRuntime(
            worker_id="worker-prod",
            store=self.store,
            registry=self.platform.workers,
            processor=self.processor(ScriptedTransport()),
            concurrency=1,
            clock=self.clock,
        )
        runtime.supervisor.track(delivery_id, claim.lease_token)
        self.clock.advance(seconds=31)
        self.claim(delivery_id)
        lost = runtime.supervisor.renew_once(now=self.clock())
        self.assertEqual((delivery_id,), lost)


class ProductionAdminCliTests(ProductionCase, unittest.TestCase):
    def invoke(self, *arguments: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {"CURSOR_SECRET": "fixture-cursor"}, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = admin_main(
                    [
                        "--db",
                        self.path,
                        "--cursor-secret-env",
                        "CURSOR_SECRET",
                        *arguments,
                    ]
                )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_health_cli_uses_production_health_service(self) -> None:
        code, stdout, stderr = self.invoke("health", "ready")
        self.assertEqual((0, ""), (code, stderr))
        self.assertEqual("ready", json.loads(stdout)["status"])

    def test_credential_cli_issues_once_and_revokes(self) -> None:
        code, stdout, stderr = self.invoke(
            "credential", "issue", "--tenant", "org-a", "--scope", "audit:read"
        )
        self.assertEqual((0, ""), (code, stderr))
        issued = json.loads(stdout)
        self.assertTrue(issued["token"].startswith(f"rfg_{issued['id']}."))
        revoked = self.invoke("credential", "revoke", issued["id"])
        self.assertEqual(0, revoked[0])
        self.assertTrue(json.loads(revoked[1])["revoked"])

    def test_consistency_and_repair_plan_cli_share_persisted_services(self) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE tenant_bucket_state SET tokens = 999 WHERE tenant_id = 'org-a'"
            )
        scanned = self.invoke("consistency", "scan")
        run_id = json.loads(scanned[1])["run_id"]
        planned = self.invoke("repair-plan", "create", run_id)
        plan_id = json.loads(planned[1])["id"]
        applied = self.invoke("repair-plan", "apply", plan_id)
        self.assertEqual("applied", json.loads(applied[1])["state"])

    def test_backup_and_restore_cli_refuse_to_echo_database_content(self) -> None:
        backup = self.root / "cli-backup.sqlite3"
        restored = self.root / "cli-restored.sqlite3"
        created = self.invoke("backup", "create", str(backup))
        self.assertEqual(0, created[0])
        self.assertNotIn("legacy-a", created[1])
        result = self.invoke("backup", "restore", str(backup), str(restored))
        self.assertEqual(0, result[0])
        self.assertTrue(restored.is_file())
