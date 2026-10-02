from __future__ import annotations

import json
from datetime import datetime

from .consistency import ConsistencyScanner
from .errors import InvalidTransition, NotFound
from .production_db import ProductionDatabase
from .production_models import RepairPlanState
from .timeutil import to_iso


class RepairExecutor:
    def __init__(self, database: ProductionDatabase) -> None:
        self.database = database
        self.scanner = ConsistencyScanner(database)

    def apply(self, plan_id: str, *, now: datetime) -> int:
        now_iso = to_iso(now)
        with self.database.write() as connection:
            plan = connection.execute(
                "SELECT * FROM repair_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise NotFound(f"repair plan {plan_id} not found")
            if plan["state"] != RepairPlanState.DRAFT.value:
                raise InvalidTransition("repair plan is not a draft")
            current = plan["source_fingerprint"]
            if current != plan["source_fingerprint"]:
                connection.execute(
                    "UPDATE repair_plans SET state = 'stale' WHERE id = ?",
                    (plan_id,),
                )
                return 0
            actions = connection.execute(
                "SELECT * FROM repair_actions WHERE plan_id = ? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
            for action in actions:
                self._apply_action(connection, action, now_iso=now_iso)
            connection.execute(
                """
                UPDATE repair_actions SET applied_at = ? WHERE plan_id = ?
                """,
                (now_iso, plan_id),
            )
            connection.execute(
                """
                UPDATE repair_plans SET state = 'applied', applied_at = ?
                WHERE id = ?
                """,
                (now_iso, plan_id),
            )
            return len(actions)

    @staticmethod
    def _apply_action(connection, action, *, now_iso: str) -> None:
        kind = action["action_type"]
        entity_id = action["entity_id"]
        expected = action["expected_version"]
        payload = json.loads(action["payload_json"])
        if kind == "clear_terminal_lease":
            _versioned(
                connection,
                """
                UPDATE deliveries SET lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, version = version + 1
                WHERE id = ? AND version = ? AND state IN ('succeeded', 'dead')
                """,
                (entity_id, expected),
            )
        elif kind == "set_attempt_count":
            _versioned(
                connection,
                """
                UPDATE deliveries SET attempt_count = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (payload["attempt_count"], entity_id, expected),
            )
        elif kind == "insert_dead_letter":
            row = connection.execute(
                "SELECT version, state FROM deliveries WHERE id = ?", (entity_id,)
            ).fetchone()
            if row is None or row["version"] != expected or row["state"] != "dead":
                raise InvalidTransition("delivery changed after repair plan creation")
            connection.execute(
                """
                INSERT INTO dead_letters(delivery_id, reason, dead_at)
                VALUES (?, ?, ?)
                """,
                (entity_id, payload["reason"], payload["dead_at"] or now_iso),
            )
        elif kind == "delete_dead_letter":
            row = connection.execute(
                "SELECT version, state FROM deliveries WHERE id = ?", (entity_id,)
            ).fetchone()
            if row is None or row["version"] != expected or row["state"] == "dead":
                raise InvalidTransition("delivery changed after repair plan creation")
            connection.execute(
                "DELETE FROM dead_letters WHERE delivery_id = ?", (entity_id,)
            )
        elif kind == "reconcile_replay_counters":
            total, processed, skipped = payload["actual"]
            changed = connection.execute(
                """
                UPDATE replay_jobs SET total_count = ?, processed_count = ?,
                    skipped_count = ?, updated_at = ? WHERE id = ?
                """,
                (total, processed, skipped, now_iso, entity_id),
            )
            if changed.rowcount != 1:
                raise InvalidTransition("replay job disappeared")
        elif kind == "clamp_bucket":
            _versioned(
                connection,
                """
                UPDATE tenant_bucket_state
                SET tokens = MIN(MAX(tokens, 0), ?), updated_at = ?,
                    version = version + 1
                WHERE tenant_id = ? AND version = ?
                """,
                (payload["burst"], now_iso, entity_id, expected),
            )
        elif kind == "reset_circuit":
            _versioned(
                connection,
                """
                UPDATE endpoint_circuits
                SET state = 'closed', failure_count = 0, opened_until = NULL,
                    probe_token = NULL, updated_at = ?, version = version + 1
                WHERE endpoint_id = ? AND version = ?
                """,
                (now_iso, entity_id, expected),
            )
        elif kind == "release_admission":
            changed = connection.execute(
                """
                UPDATE dispatch_admissions SET state = 'released',
                    reason = 'consistency_repair', updated_at = ?
                WHERE delivery_id = ? AND state = 'admitted'
                """,
                (now_iso, entity_id),
            )
            if changed.rowcount != 1:
                raise InvalidTransition("dispatch admission changed")
        else:
            raise InvalidTransition(f"unknown repair action {kind}")


def _versioned(connection, sql: str, parameters: tuple[object, ...]) -> None:
    changed = connection.execute(sql, parameters)
    if changed.rowcount != 1:
        raise InvalidTransition("entity version changed after repair plan creation")
