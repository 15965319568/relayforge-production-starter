from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta
from pathlib import Path

from _enterprise_support import EnterpriseCase, invoke_wsgi
from _support import BASE_TIME
from relayforge.management_api import ManagementApp
from relayforge.settings import load_settings


class ManagementApiTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = ManagementApp(self.platform, clock=lambda: BASE_TIME)

    def test_health_returns_json_and_no_store_cache_headers(self) -> None:
        status, payload, headers = invoke_wsgi(self.app, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn(("Cache-Control", "no-store"), headers)

    def test_create_subscription_route(self) -> None:
        status, payload, _ = invoke_wsgi(
            self.app,
            "POST",
            "/v1/subscriptions",
            body={
                "tenant_id": "org-a",
                "endpoint_id": "ep-a",
                "topic_pattern": "invoice.*",
                "filter": {"amount": {"$gte": 100}},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["topic_pattern"], "invoice.*")

    def test_secret_rotation_reads_indirect_secret_and_never_echoes_it(self) -> None:
        status, payload, _ = invoke_wsgi(
            self.app,
            "POST",
            "/v1/secrets/ep-a/rotate",
            body={"secret_env": "NEXT_SECRET", "overlap_seconds": 60},
            secret_environ={"NEXT_SECRET": "never-echo-this"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["key_id"], "v2")
        self.assertNotIn("never-echo-this", json.dumps(payload))

    def test_missing_secret_environment_is_expected_error(self) -> None:
        status, payload, _ = invoke_wsgi(
            self.app,
            "POST",
            "/v1/secrets/ep-a/rotate",
            body={"secret_env": "MISSING"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "ValidationError")

    def test_audit_route_uses_signed_pagination_cursor(self) -> None:
        delivery = self.make_succeeded()
        for index in range(2):
            self.store.append_audit(
                occurred_at=BASE_TIME + timedelta(seconds=index + 2),
                kind="request",
                delivery_id=delivery,
                details={"index": index},
            )
        status, first, _ = invoke_wsgi(
            self.app, "GET", "/v1/audit", query="tenant_id=org-a&limit=1"
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(first["next_cursor"])
        status, second, _ = invoke_wsgi(
            self.app,
            "GET",
            "/v1/audit",
            query=f"tenant_id=org-a&limit=1&cursor={first['next_cursor']}",
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_create_and_run_replay_job(self) -> None:
        delivery = self.make_dead()
        status, job, _ = invoke_wsgi(
            self.app,
            "POST",
            "/v1/replay-jobs",
            body={"tenant_id": "org-a"},
            headers={"Idempotency-Key": "replay-api-1"},
        )
        self.assertEqual(status, 201)
        status, result, _ = invoke_wsgi(
            self.app,
            "POST",
            f"/v1/replay-jobs/{job['id']}/run",
            body={"limit": 10},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.store.inspect_delivery(delivery)["state"], "pending")

    def test_replay_idempotency_conflict_returns_409(self) -> None:
        self.make_dead()
        request = dict(
            method="POST",
            path="/v1/replay-jobs",
            headers={"Idempotency-Key": "same"},
        )
        invoke_wsgi(self.app, body={"tenant_id": "org-a"}, **request)
        status, payload, _ = invoke_wsgi(
            self.app, body={"tenant_id": "org-b"}, **request
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "IdempotencyConflict")

    def test_unknown_route_and_invalid_json_are_structured_errors(self) -> None:
        status, payload, _ = invoke_wsgi(self.app, "GET", "/missing")
        self.assertEqual((status, payload["error"]), (404, "NotFound"))
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/subscriptions",
            "CONTENT_LENGTH": "1",
            "wsgi.input": __import__("io").BytesIO(b"{"),
        }
        captured = {}
        chunks = self.app(
            environ,
            lambda status, headers: captured.update(status=status, headers=headers),
        )
        self.assertTrue(captured["status"].startswith("400"))
        self.assertEqual(json.loads(b"".join(chunks))["error"], "JSONDecodeError")

    def test_worker_registration_returns_generation_not_metadata_secrets(self) -> None:
        status, payload, _ = invoke_wsgi(
            self.app,
            "POST",
            "/v1/workers/register",
            body={"worker_id": "worker-1", "metadata": {"zone": "cn-east"}},
        )
        self.assertEqual(status, 201)
        self.assertIn("generation", payload)
        self.assertNotIn("metadata", payload)


class SettingsTests(unittest.TestCase):
    def test_loads_toml_and_environment_database_override(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            path = Path(temp) / "relayforge.toml"
            path.write_text(
                """
[service]
database_path = "default.sqlite3"
cursor_secret_env = "CURSOR_SECRET"
busy_timeout_ms = 7000
audit_cursor_ttl_seconds = 300
worker_stale_after_seconds = 90
""".strip(),
                encoding="utf-8",
            )
            settings = load_settings(
                path, environ={"RELAYFORGE_DATABASE": "override.sqlite3"}
            )
        self.assertEqual(settings.database_path, "override.sqlite3")
        self.assertEqual(settings.busy_timeout_ms, 7000)

    def test_cursor_secret_is_loaded_indirectly(self) -> None:
        from relayforge.settings import PlatformSettings

        settings = PlatformSettings("db.sqlite3", "CURSOR_SECRET")
        self.assertEqual(
            settings.cursor_secret({"CURSOR_SECRET": "fixture-secret"}),
            "fixture-secret",
        )

    def test_invalid_toml_types_are_rejected(self) -> None:
        from tempfile import TemporaryDirectory
        from relayforge.errors import ValidationError

        with TemporaryDirectory() as temp:
            path = Path(temp) / "relayforge.toml"
            path.write_text(
                '[service]\ndatabase_path="db"\ncursor_secret_env="SECRET"\nbusy_timeout_ms="slow"',
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError):
                load_settings(path)


if __name__ == "__main__":
    unittest.main()

