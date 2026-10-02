from __future__ import annotations

import csv
import json
import unittest

from _support import ROOT


class FixtureIntegrityTests(unittest.TestCase):
    def test_demo_fixture_ids_and_idempotency_keys_are_unique(self) -> None:
        endpoints = json.loads(
            (ROOT / "fixtures" / "endpoints.json").read_text(encoding="utf-8")
        )
        events = _json_lines(ROOT / "fixtures" / "events.jsonl")
        self.assertEqual(len({row["id"] for row in endpoints}), len(endpoints))
        self.assertEqual(len({row["id"] for row in events}), len(events))
        keys = {(row["tenant_id"], row["idempotency_key"]) for row in events}
        self.assertEqual(len(keys), len(events))

    def test_demo_events_only_target_tenants_with_endpoints(self) -> None:
        endpoints = json.loads(
            (ROOT / "fixtures" / "endpoints.json").read_text(encoding="utf-8")
        )
        events = _json_lines(ROOT / "fixtures" / "events.jsonl")
        tenants = {row["tenant_id"] for row in endpoints}
        self.assertTrue({row["tenant_id"] for row in events}.issubset(tenants))

    def test_fixture_urls_and_secrets_are_non_production_markers(self) -> None:
        endpoints = json.loads(
            (ROOT / "fixtures" / "endpoints.json").read_text(encoding="utf-8")
        )
        for endpoint in endpoints:
            self.assertIn(".example.net", endpoint["url"])
            self.assertTrue(endpoint["secret"].startswith("fixture_key_"))

    def test_incident_artifacts_have_consistent_cross_file_identifiers(self) -> None:
        snapshot = ROOT / "fixtures" / "incident_snapshot"
        deliveries = _json_lines(snapshot / "delivery_export.ndjson")
        timeline = _json_lines(snapshot / "worker_timeline.ndjson")
        with (snapshot / "tenant_backlog.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            backlog = list(csv.DictReader(stream))
        delivery_ids = {row["delivery_id"] for row in deliveries}
        timeline_ids = {
            row["delivery_id"] for row in timeline if "delivery_id" in row
        }
        backlog_tenants = {row["tenant_id"] for row in backlog}
        export_tenants = {row["tenant_id"] for row in deliveries}
        self.assertTrue(timeline_ids.issubset(delivery_ids))
        self.assertTrue(export_tenants.issubset(backlog_tenants))

    def test_control_incident_artifacts_parse_and_share_identifiers(self) -> None:
        import csv

        snapshot = ROOT / "fixtures" / "control_incident"
        with (snapshot / "subscriptions.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            subscriptions = list(csv.DictReader(stream))
        admission = _json_lines(snapshot / "admission_timeline.ndjson")
        replay = _json_lines(snapshot / "replay_requests.ndjson")
        api = _json_lines(snapshot / "api_trace.ndjson")
        endpoint_ids = {row["endpoint_id"] for row in subscriptions}
        observed_endpoints = {
            row["endpoint_id"] for row in admission if "endpoint_id" in row
        }
        self.assertTrue(observed_endpoints.issubset(endpoint_ids))
        replay_keys = {
            row["idempotency_key"] for row in replay if "idempotency_key" in row
        }
        self.assertEqual(replay_keys, {"replay-org7d83-20261003"})
        self.assertTrue(any(row.get("cursor_alias") == "cursor-A" for row in api))

    def test_production_incident_is_synthetic_parseable_and_correlated(self) -> None:
        root = ROOT / "fixtures" / "production_incident"
        readme = (root / "README.md").read_text(encoding="utf-8").casefold()
        self.assertIn("fully synthetic", readme)
        with (root / "dispatch_admissions.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            admissions = list(csv.DictReader(stream))
        workers = _json_lines(root / "worker_timeline.ndjson")
        auth = _json_lines(root / "auth_requests.ndjson")
        maintenance = _json_lines(root / "maintenance_runs.ndjson")
        with (root / "backup_catalog.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            backups = list(csv.DictReader(stream))
        delivery_ids = {item["delivery_id"] for item in admissions}
        timeline_ids = {
            item["delivery_id"] for item in workers if item.get("delivery_id")
        }
        backup_ids = {item["backup_id"] for item in backups}
        maintenance_backup_ids = {
            item["backup_id"] for item in maintenance if item.get("backup_id")
        }
        self.assertTrue(timeline_ids.issubset(delivery_ids))
        self.assertTrue(maintenance_backup_ids.issubset(backup_ids))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in backups))
        rendered = json.dumps([admissions, workers, auth, maintenance, backups])
        self.assertNotIn("Bearer ", rendered)
        self.assertNotIn("query-secret", rendered)


def _json_lines(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


if __name__ == "__main__":
    unittest.main()
