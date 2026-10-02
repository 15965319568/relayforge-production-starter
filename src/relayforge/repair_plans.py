from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Iterable

from .errors import NotFound, ValidationError
from .production_db import ProductionDatabase
from .production_models import RepairAction, RepairPlan, RepairPlanState
from .timeutil import from_iso, to_iso


REPAIRABLE_CODES = frozenset(
    {
        "TERMINAL_ACTIVE_LEASE",
        "ATTEMPT_COUNT_BEHIND",
        "DEAD_MISSING_LETTER",
        "NON_DEAD_HAS_LETTER",
        "REPLAY_COUNTER_MISMATCH",
        "BUCKET_OUT_OF_RANGE",
        "CIRCUIT_PROBE_MISMATCH",
        "STALE_DISPATCH_ADMISSION",
    }
)


class RepairPlanService:
    def __init__(self, database: ProductionDatabase) -> None:
        self.database = database

    def create(
        self,
        source_run_id: str,
        *,
        now: datetime,
        codes: Iterable[str] | None = None,
        plan_id: str | None = None,
    ) -> RepairPlan:
        requested = set(codes or REPAIRABLE_CODES)
        unsupported = requested - REPAIRABLE_CODES
        if unsupported:
            raise ValidationError(f"unsupported repair codes: {sorted(unsupported)}")
        identifier = plan_id or f"repair_{uuid.uuid4().hex}"
        with self.database.write() as connection:
            run = connection.execute(
                "SELECT * FROM consistency_runs WHERE id = ?", (source_run_id,)
            ).fetchone()
            if run is None or not run["fingerprint"]:
                raise NotFound(f"completed consistency run {source_run_id} not found")
            findings = connection.execute(
                """
                SELECT * FROM consistency_findings
                WHERE run_id = ? ORDER BY ordinal
                """,
                (source_run_id,),
            ).fetchall()
            actions = [
                action
                for finding in findings
                if finding["code"] in requested
                for action in _actions_for(finding)
            ]
            connection.execute(
                """
                INSERT INTO repair_plans(
                    id, source_run_id, source_fingerprint, state,
                    action_count, created_at
                ) VALUES (?, ?, ?, 'draft', ?, ?)
                """,
                (
                    identifier,
                    source_run_id,
                    run["fingerprint"],
                    len(actions),
                    to_iso(now),
                ),
            )
            for ordinal, action in enumerate(actions, 1):
                connection.execute(
                    """
                    INSERT INTO repair_actions(
                        plan_id, ordinal, action_type, entity_type, entity_id,
                        expected_version, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        ordinal,
                        action.action_type,
                        action.entity_type,
                        action.entity_id,
                        action.expected_version,
                        json.dumps(
                            action.payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
        return RepairPlan(
            identifier,
            source_run_id,
            run["fingerprint"],
            RepairPlanState.DRAFT,
            len(actions),
            now,
            None,
        )

    def get(self, plan_id: str) -> tuple[RepairPlan, tuple[RepairAction, ...]]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM repair_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"repair plan {plan_id} not found")
            actions = connection.execute(
                "SELECT * FROM repair_actions WHERE plan_id = ? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
        return _plan(row), tuple(_action(item) for item in actions)

    def cancel(self, plan_id: str) -> None:
        with self.database.write() as connection:
            changed = connection.execute(
                """
                UPDATE repair_plans SET state = 'cancelled'
                WHERE id = ? AND state = 'draft'
                """,
                (plan_id,),
            )
            if changed.rowcount != 1:
                raise ValidationError("only a draft repair plan can be cancelled")


def _actions_for(row) -> list[RepairAction]:
    details = json.loads(row["details_json"])
    version = details.get("expected_version")
    code = row["code"]
    if code == "TERMINAL_ACTIVE_LEASE":
        action_type, payload = "clear_terminal_lease", {}
    elif code == "ATTEMPT_COUNT_BEHIND":
        action_type = "set_attempt_count"
        payload = {"attempt_count": details["highest_ordinal"]}
    elif code == "DEAD_MISSING_LETTER":
        action_type = "insert_dead_letter"
        payload = {"reason": details["reason"], "dead_at": details["dead_at"]}
    elif code == "NON_DEAD_HAS_LETTER":
        action_type, payload = "delete_dead_letter", {}
    elif code == "REPLAY_COUNTER_MISMATCH":
        action_type, payload = "reconcile_replay_counters", {"actual": details["actual"]}
    elif code == "BUCKET_OUT_OF_RANGE":
        action_type, payload = "clamp_bucket", {"burst": details["burst"]}
    elif code == "CIRCUIT_PROBE_MISMATCH":
        action_type, payload = "reset_circuit", {}
    elif code == "STALE_DISPATCH_ADMISSION":
        action_type, payload = "release_admission", {}
    else:
        raise ValidationError(f"unsupported repair finding {code}")
    return [
        RepairAction(
            0,
            action_type,
            row["entity_type"],
            row["entity_id"],
            int(version) if version is not None else None,
            payload,
        )
    ]


def _plan(row) -> RepairPlan:
    return RepairPlan(
        row["id"],
        row["source_run_id"],
        row["source_fingerprint"],
        RepairPlanState(row["state"]),
        row["action_count"],
        from_iso(row["created_at"]),
        from_iso(row["applied_at"]) if row["applied_at"] else None,
    )


def _action(row) -> RepairAction:
    return RepairAction(
        row["ordinal"],
        row["action_type"],
        row["entity_type"],
        row["entity_id"],
        row["expected_version"],
        json.loads(row["payload_json"]),
        from_iso(row["applied_at"]) if row["applied_at"] else None,
    )
