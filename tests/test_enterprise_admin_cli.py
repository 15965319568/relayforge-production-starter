from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from _enterprise_support import EnterpriseCase
from _support import BASE_TIME
from relayforge.admin_cli import main
from relayforge.diagnostics import DiagnosticService


class AdminCliTests(EnterpriseCase, unittest.TestCase):
    def invoke(self, *args: str, environment=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        values = {"CURSOR_SECRET": "cursor-secret", **(environment or {})}
        with patch.dict(os.environ, values, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "--db",
                        self.path,
                        "--cursor-secret-env",
                        "CURSOR_SECRET",
                        *args,
                    ]
                )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_subscription_add_and_list_are_json_only(self) -> None:
        added = self.invoke(
            "subscription",
            "add",
            "--tenant",
            "org-a",
            "--endpoint",
            "ep-a",
            "--topic",
            "invoice.*",
            "--filter",
            '{"amount":{"$gte":100}}',
        )
        self.assertEqual((added[0], added[2]), (0, ""))
        self.assertEqual(json.loads(added[1])["topic_pattern"], "invoice.*")
        listed = self.invoke("subscription", "list", "--tenant", "org-a")
        self.assertEqual(listed[0], 0)
        self.assertGreaterEqual(len(json.loads(listed[1])), 3)

    def test_secret_rotate_never_echoes_secret(self) -> None:
        code, stdout, stderr = self.invoke(
            "secret",
            "rotate",
            "--endpoint",
            "ep-a",
            "--secret-env",
            "NEXT_SECRET",
            environment={"NEXT_SECRET": "never-print-this"},
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertNotIn("never-print-this", stdout)

    def test_rate_set_validates_and_prints_policy(self) -> None:
        code, stdout, stderr = self.invoke(
            "rate",
            "set",
            "--tenant",
            "org-a",
            "--per-second",
            "3.5",
            "--burst",
            "8",
            "--max-in-flight",
            "4",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["max_in_flight"], 4)

    def test_invalid_filter_is_exit_two_and_empty_stdout(self) -> None:
        code, stdout, stderr = self.invoke(
            "subscription",
            "add",
            "--tenant",
            "org-a",
            "--endpoint",
            "ep-a",
            "--topic",
            "invoice.*",
            "--filter",
            "[]",
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"], "ValidationError")

    def test_audit_rejects_non_z_timestamp(self) -> None:
        code, stdout, stderr = self.invoke(
            "audit", "export", "--after", "2026-09-20"
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertEqual(json.loads(stderr)["error"], "ValueError")

    def test_worker_registration_does_not_echo_metadata(self) -> None:
        code, stdout, stderr = self.invoke(
            "worker",
            "register",
            "--worker",
            "worker-1",
            "--metadata",
            '{"zone":"cn-east","credential":"do-not-print"}',
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertNotIn("do-not-print", stdout)

    def test_missing_cursor_secret_is_expected_error(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "--db",
                        self.path,
                        "--cursor-secret-env",
                        "MISSING",
                        "diagnose",
                    ]
                )
        self.assertEqual((code, stdout.getvalue()), (2, ""))
        self.assertEqual(json.loads(stderr.getvalue())["error"], "ValidationError")


class DiagnosticTests(EnterpriseCase, unittest.TestCase):
    def test_snapshot_aggregates_without_secrets_or_tokens(self) -> None:
        self.make_dead()
        self.platform.workers.register("worker-1", now=BASE_TIME, metadata={"zone": "a"})
        snapshot = DiagnosticService(self.database).snapshot(
            now=BASE_TIME, stale_worker_seconds=10
        )
        rendered = json.dumps(snapshot)
        self.assertIn("dead", snapshot["deliveries"])
        self.assertIn("org-a", {row["tenant_id"] for row in snapshot["tenants"]})
        for forbidden in ("secret-a", "fixture", "lease_token"):
            self.assertNotIn(forbidden, rendered)

    def test_stale_worker_count_uses_inclusive_boundary(self) -> None:
        self.platform.workers.register("worker-1", now=BASE_TIME)
        snapshot = DiagnosticService(self.database).snapshot(
            now=BASE_TIME, stale_worker_seconds=0
        )
        self.assertEqual(snapshot["stale_workers"], 1)


if __name__ == "__main__":
    unittest.main()

