from __future__ import annotations

from datetime import datetime

from .control_db import ControlDatabase, new_control_id
from .control_models import RetentionPlan, RetentionResult
from .errors import ValidationError
from .timeutil import to_iso


class RetentionService:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def plan(
        self, *, response_before: datetime, audit_before: datetime
    ) -> RetentionPlan:
        response_iso = to_iso(response_before)
        audit_iso = to_iso(audit_before)
        with self.database.connection() as connection:
            excerpts = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM attempts attempt
                JOIN deliveries delivery ON delivery.id = attempt.delivery_id
                WHERE attempt.finished_at < ?
                  AND attempt.response_excerpt IS NOT NULL
                """,
                (response_iso,),
            ).fetchone()["count"]
            audit = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM audit_log audit
                LEFT JOIN deliveries delivery ON delivery.id = audit.delivery_id
                WHERE audit.occurred_at < ?
                """,
                (audit_iso,),
            ).fetchone()["count"]
        return RetentionPlan(excerpts, audit)

    def apply(
        self,
        *,
        response_before: datetime,
        audit_before: datetime,
        now: datetime,
        dry_run: bool = False,
    ) -> RetentionResult:
        if response_before > now or audit_before > now:
            raise ValidationError("retention cutoffs cannot be in the future")
        plan = self.plan(response_before=response_before, audit_before=audit_before)
        if dry_run:
            return RetentionResult(plan.response_excerpts, plan.audit_rows, True)
        response_iso = to_iso(response_before)
        audit_iso = to_iso(audit_before)
        with self.database.write() as connection:
            cleared = connection.execute(
                """
                UPDATE attempts SET response_excerpt = NULL
                WHERE id IN (
                    SELECT attempt.id
                    FROM attempts attempt
                    JOIN deliveries delivery ON delivery.id = attempt.delivery_id
                    WHERE attempt.finished_at < ?
                      AND attempt.response_excerpt IS NOT NULL
                )
                """,
                (response_iso,),
            ).rowcount
            deleted = connection.execute(
                """
                DELETE FROM audit_log
                WHERE id IN (
                    SELECT audit.id
                    FROM audit_log audit
                    LEFT JOIN deliveries delivery ON delivery.id = audit.delivery_id
                    WHERE audit.occurred_at < ?
                )
                """,
                (audit_iso,),
            ).rowcount
            connection.execute(
                """
                INSERT INTO retention_runs(
                    id, response_before, audit_before, response_excerpts_cleared,
                    audit_rows_deleted, dry_run, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    new_control_id("ret"),
                    response_iso,
                    audit_iso,
                    cleared,
                    deleted,
                    to_iso(now),
                ),
            )
        return RetentionResult(cleared, deleted, False)
