from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .control_db import ControlDatabase, new_control_id
from .control_models import ReplayBatchResult, ReplayJob, ReplayJobState
from .errors import IdempotencyConflict, InvalidTransition, NotFound, ValidationError
from .timeutil import from_iso, to_iso


class ReplayJobService:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def create(
        self,
        *,
        idempotency_key: str,
        now: datetime,
        tenant_id: str | None = None,
        reason: str | None = None,
        before: datetime | None = None,
        job_id: str | None = None,
    ) -> tuple[ReplayJob, bool]:
        key = idempotency_key.strip()
        if not key:
            raise ValidationError("idempotency_key is required")
        request = {
            "tenant_id": tenant_id,
            "reason": reason,
            "before_at": to_iso(before) if before else None,
        }
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        identifier = job_id or new_control_id("rpl")
        now_iso = to_iso(now)
        with self.database.write() as connection:
            existing = connection.execute(
                "SELECT * FROM replay_jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                return _job(existing), False
            clauses = ["delivery.state = 'dead'"]
            params: list[object] = []
            if tenant_id is not None:
                clauses.append("event.tenant_id = ?")
                params.append(tenant_id)
            if reason is not None:
                clauses.append("dead.reason = ?")
                params.append(reason)
            if before is not None:
                clauses.append("dead.dead_at < ?")
                params.append(to_iso(before))
            rows = connection.execute(
                f"""
                SELECT dead.delivery_id
                FROM dead_letters dead
                JOIN deliveries delivery ON delivery.id = dead.delivery_id
                JOIN events event ON event.id = delivery.event_id
                WHERE {' AND '.join(clauses)}
                ORDER BY dead.dead_at, dead.delivery_id
                """,
                params,
            ).fetchall()
            state = ReplayJobState.PENDING if rows else ReplayJobState.COMPLETED
            connection.execute(
                """
                INSERT INTO replay_jobs(
                    id, idempotency_key, request_hash, tenant_id, reason,
                    before_at, state, total_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    key,
                    request_hash,
                    tenant_id,
                    reason,
                    to_iso(before) if before else None,
                    state.value,
                    len(rows),
                    now_iso,
                    now_iso,
                ),
            )
            for ordinal, row in enumerate(rows, start=1):
                connection.execute(
                    """
                    INSERT INTO replay_job_items(job_id, delivery_id, ordinal, state)
                    VALUES (?, ?, ?, 'pending')
                    """,
                    (identifier, row["delivery_id"], ordinal),
                )
            created = connection.execute(
                "SELECT * FROM replay_jobs WHERE id = ?", (identifier,)
            ).fetchone()
        return _job(created), True

    def get(self, job_id: str) -> ReplayJob:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM replay_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"replay job {job_id} not found")
        return _job(row)

    def run_batch(
        self, job_id: str, *, now: datetime, limit: int = 100
    ) -> ReplayBatchResult:
        if not 1 <= limit <= 500:
            raise ValidationError("replay batch limit must be between 1 and 500")
        now_iso = to_iso(now)
        with self.database.write() as connection:
            job = connection.execute(
                "SELECT * FROM replay_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFound(f"replay job {job_id} not found")
            state = ReplayJobState(job["state"])
            if state is ReplayJobState.CANCELLED:
                raise InvalidTransition("cancelled replay job cannot run")
            if state is ReplayJobState.COMPLETED:
                return ReplayBatchResult(job_id, 0, 0, 0, state)
            items = connection.execute(
                """
                SELECT * FROM replay_job_items
                WHERE job_id = ? AND state = 'pending'
                ORDER BY ordinal LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
            applied = 0
            skipped = 0
            for item in items:
                delivery = connection.execute(
                    "SELECT state FROM deliveries WHERE id = ?",
                    (item["delivery_id"],),
                ).fetchone()
                if delivery is not None and delivery["state"] == "dead":
                    connection.execute(
                        """
                        UPDATE deliveries
                        SET state = 'pending', next_attempt_at = ?, completed_at = NULL,
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, last_error = NULL,
                            last_status = NULL, version = version + 1
                        WHERE id = ? AND state = 'dead'
                        """,
                        (now_iso, item["delivery_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE dead_letters
                        SET replay_count = replay_count + 1, last_replayed_at = ?
                        WHERE delivery_id = ?
                        """,
                        (now_iso, item["delivery_id"]),
                    )
                    item_state = "applied"
                    applied += 1
                else:
                    item_state = "skipped"
                    skipped += 1
                connection.execute(
                    """
                    UPDATE replay_job_items
                    SET state = ?, processed_at = ?
                    WHERE job_id = ? AND delivery_id = ? AND state = 'pending'
                    """,
                    (item_state, now_iso, job_id, item["delivery_id"]),
                )
            remaining = connection.execute(
                """
                SELECT COUNT(*) AS count FROM replay_job_items
                WHERE job_id = ? AND state = 'pending'
                """,
                (job_id,),
            ).fetchone()["count"]
            new_state = (
                ReplayJobState.COMPLETED if remaining == 0 else ReplayJobState.RUNNING
            )
            connection.execute(
                """
                UPDATE replay_jobs
                SET state = ?, processed_count = processed_count + ?,
                    skipped_count = skipped_count + ?, updated_at = ?
                WHERE id = ?
                """,
                (new_state.value, applied, skipped, now_iso, job_id),
            )
        return ReplayBatchResult(job_id, applied, skipped, remaining, new_state)

    def cancel(self, job_id: str, *, now: datetime) -> None:
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT state FROM replay_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"replay job {job_id} not found")
            if row["state"] == ReplayJobState.COMPLETED.value:
                raise InvalidTransition("completed replay job cannot be cancelled")
            connection.execute(
                "UPDATE replay_jobs SET state = 'cancelled', updated_at = ? WHERE id = ?",
                (to_iso(now), job_id),
            )


def _job(row) -> ReplayJob:
    return ReplayJob(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        tenant_id=row["tenant_id"],
        reason=row["reason"],
        before_at=from_iso(row["before_at"]) if row["before_at"] else None,
        state=ReplayJobState(row["state"]),
        total_count=row["total_count"],
        processed_count=row["processed_count"],
        skipped_count=row["skipped_count"],
        last_error=row["last_error"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )
