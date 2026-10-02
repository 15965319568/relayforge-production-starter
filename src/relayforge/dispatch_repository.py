from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from .errors import InvalidTransition, LeaseLost, NotFound, ValidationError
from .production_db import ProductionDatabase
from .production_models import AdmissionResult, DispatchOutcome
from .redaction import safe_excerpt
from .timeutil import from_iso, to_iso


class DispatchRepository:
    """Owns dispatch state that must change in one SQLite transaction."""

    def __init__(
        self,
        database: ProductionDatabase,
        *,
        circuit_failure_threshold: int = 5,
        circuit_recovery_seconds: int = 60,
    ) -> None:
        if circuit_failure_threshold <= 0 or circuit_recovery_seconds <= 0:
            raise ValueError("circuit settings must be positive")
        self.database = database
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_recovery_seconds = circuit_recovery_seconds

    def admit(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        now: datetime,
    ) -> AdmissionResult:
        now_iso = to_iso(now)
        token_hash = _token_hash(lease_token)
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT d.id, d.state, d.lease_token, d.lease_expires_at,
                       d.endpoint_id, e.tenant_id
                FROM deliveries d JOIN events e ON e.id = d.event_id
                WHERE d.id = ?
                """,
                (delivery_id,),
            ).fetchone()
            self._require_live(row, lease_token, now_iso)

            existing = connection.execute(
                "SELECT * FROM dispatch_admissions WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["state"] == "admitted"
                and existing["lease_token_hash"] == token_hash
            ):
                return AdmissionResult(
                    delivery_id, True, None, None, existing["probe_token"]
                )

            policy = connection.execute(
                "SELECT * FROM tenant_rate_policies WHERE tenant_id = ?",
                (row["tenant_id"],),
            ).fetchone()
            bucket = connection.execute(
                "SELECT * FROM tenant_bucket_state WHERE tenant_id = ?",
                (row["tenant_id"],),
            ).fetchone()
            circuit = connection.execute(
                "SELECT * FROM endpoint_circuits WHERE endpoint_id = ?",
                (row["endpoint_id"],),
            ).fetchone()
            if policy is None or bucket is None:
                raise NotFound(f"rate policy for tenant {row['tenant_id']} not found")
            if circuit is None:
                raise NotFound(f"circuit for endpoint {row['endpoint_id']} not found")

            elapsed = max(
                0.0, (now - from_iso(bucket["updated_at"])).total_seconds()
            )
            available = min(
                float(policy["burst"]),
                float(bucket["tokens"])
                + elapsed * float(policy["rate_per_second"]),
            )
            tenant_rank = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM deliveries active
                JOIN events event ON event.id = active.event_id
                WHERE event.tenant_id = ? AND active.state = 'in_flight'
                  AND active.lease_expires_at > ?
                  AND (
                    active.lease_expires_at < ? OR
                    (active.lease_expires_at = ? AND active.id <= ?)
                  )
                """,
                (
                    row["tenant_id"],
                    now_iso,
                    row["lease_expires_at"],
                    row["lease_expires_at"],
                    delivery_id,
                ),
            ).fetchone()["count"]

            reason: str | None = None
            retry_at: datetime | None = None
            if tenant_rank > int(policy["max_in_flight"]):
                reason = "tenant_capacity"
                retry_at = now + timedelta(seconds=1)
            elif available + 1e-12 < 1.0:
                reason = "rate_limited"
                wait = (1.0 - available) / float(policy["rate_per_second"])
                retry_at = now + timedelta(seconds=max(0.001, wait))

            probe_token: str | None = None
            circuit_state = circuit["state"]
            opened_until = (
                from_iso(circuit["opened_until"])
                if circuit["opened_until"]
                else None
            )
            if reason is None:
                if (
                    circuit_state == "open"
                    and opened_until is not None
                    and opened_until > now
                ):
                    reason = "circuit_open"
                    retry_at = opened_until
                elif circuit_state == "half_open" and circuit["probe_token"]:
                    reason = "circuit_probe_busy"
                    retry_at = opened_until or now + timedelta(seconds=1)
                elif circuit_state != "closed":
                    probe_token = uuid.uuid4().hex

            if reason is not None:
                connection.execute(
                    """
                    UPDATE tenant_bucket_state
                    SET tokens = ?, updated_at = ?, version = version + 1
                    WHERE tenant_id = ?
                    """,
                    (max(0.0, available - 1.0), now_iso, row["tenant_id"]),
                )
                self._defer(
                    connection,
                    delivery_id,
                    lease_token,
                    next_attempt_at=retry_at or now + timedelta(seconds=1),
                    reason=reason,
                )
                self._upsert_admission(
                    connection,
                    delivery_id=delivery_id,
                    tenant_id=row["tenant_id"],
                    endpoint_id=row["endpoint_id"],
                    token_hash=token_hash,
                    rate_reserved=0.0,
                    probe_token=None,
                    state="released",
                    reason=reason,
                    now_iso=now_iso,
                )
                return AdmissionResult(delivery_id, False, reason, retry_at, None)

            connection.execute(
                """
                UPDATE tenant_bucket_state
                SET tokens = ?, updated_at = ?, version = version + 1
                WHERE tenant_id = ?
                """,
                (max(0.0, available - 1.0), now_iso, row["tenant_id"]),
            )
            if probe_token is not None:
                connection.execute(
                    """
                    UPDATE endpoint_circuits
                    SET state = 'half_open', probe_token = ?, updated_at = ?,
                        version = version + 1
                    WHERE endpoint_id = ?
                    """,
                    (probe_token, now_iso, row["endpoint_id"]),
                )
            self._upsert_admission(
                connection,
                delivery_id=delivery_id,
                tenant_id=row["tenant_id"],
                endpoint_id=row["endpoint_id"],
                token_hash=token_hash,
                rate_reserved=1.0,
                probe_token=probe_token,
                state="admitted",
                reason=None,
                now_iso=now_iso,
            )
            return AdmissionResult(delivery_id, True, None, None, probe_token)

    def start_attempt(
        self, delivery_id: str, lease_token: str, *, now: datetime
    ) -> int:
        now_iso = to_iso(now)
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            self._require_live(row, lease_token, now_iso)
            admission = connection.execute(
                "SELECT * FROM dispatch_admissions WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if (
                admission is None
                or admission["state"] != "admitted"
                or admission["lease_token_hash"] != _token_hash(lease_token)
            ):
                raise InvalidTransition("delivery does not have a live admission")
            existing = connection.execute(
                """
                SELECT ordinal FROM attempts
                WHERE delivery_id = ? AND lease_token = ?
                """,
                (delivery_id, lease_token),
            ).fetchone()
            if existing is not None:
                return int(existing["ordinal"])
            ordinal = int(row["attempt_count"]) + 1
            connection.execute(
                """
                INSERT INTO attempts(
                    delivery_id, ordinal, lease_token, started_at, outcome
                ) VALUES (?, ?, ?, ?, 'started')
                """,
                (delivery_id, ordinal, lease_token, now_iso),
            )
            connection.execute(
                """
                UPDATE deliveries
                SET attempt_count = ?, first_attempt_at = COALESCE(first_attempt_at, ?),
                    version = version + 1
                WHERE id = ?
                """,
                (ordinal, now_iso, delivery_id),
            )
            return ordinal

    def finish(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        now: datetime,
        outcome: DispatchOutcome,
        status_code: int | None,
        error_kind: str | None,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
        next_attempt_at: datetime | None = None,
    ) -> None:
        if outcome not in {
            DispatchOutcome.SUCCEEDED,
            DispatchOutcome.RETRIED,
            DispatchOutcome.DEAD,
        }:
            raise ValidationError("finish requires a terminal attempt outcome")
        if outcome is DispatchOutcome.RETRIED and next_attempt_at is None:
            raise ValidationError("retried outcome requires next_attempt_at")
        now_iso = to_iso(now)
        with self.database.write() as connection:
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            self._require_live(delivery, lease_token, now_iso)
            admission = connection.execute(
                "SELECT * FROM dispatch_admissions WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if (
                admission is None
                or admission["state"] != "admitted"
                or admission["lease_token_hash"] != _token_hash(lease_token)
            ):
                raise LeaseLost("dispatch admission is stale")
            attempt_outcome = {
                DispatchOutcome.SUCCEEDED: "succeeded",
                DispatchOutcome.RETRIED: "retryable",
                DispatchOutcome.DEAD: (
                    "terminal"
                    if status_code is not None and status_code < 500
                    else "retryable"
                ),
            }[outcome]
            updated = connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, status_code = ?, error_kind = ?,
                    retry_after = ?, request_id = ?, response_excerpt = ?
                WHERE delivery_id = ? AND lease_token = ? AND outcome = 'started'
                """,
                (
                    now_iso,
                    attempt_outcome,
                    status_code,
                    error_kind,
                    retry_after,
                    request_id,
                    safe_excerpt(response_body),
                    delivery_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransition("attempt is missing or already finished")

            if outcome is DispatchOutcome.SUCCEEDED:
                connection.execute(
                    """
                    UPDATE deliveries SET state = 'succeeded', completed_at = ?,
                        last_status = ?, last_error = NULL, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        version = version + 1 WHERE id = ?
                    """,
                    (now_iso, status_code, delivery_id),
                )
            elif outcome is DispatchOutcome.RETRIED:
                connection.execute(
                    """
                    UPDATE deliveries SET state = 'retry_wait', next_attempt_at = ?,
                        completed_at = NULL, last_status = ?, last_error = ?,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, version = version + 1 WHERE id = ?
                    """,
                    (
                        to_iso(next_attempt_at),
                        status_code,
                        error_kind,
                        delivery_id,
                    ),
                )
            else:
                reason = error_kind or "delivery_failed"
                connection.execute(
                    """
                    UPDATE deliveries SET state = 'dead', completed_at = ?,
                        last_status = ?, last_error = ?, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        version = version + 1 WHERE id = ?
                    """,
                    (now_iso, status_code, reason, delivery_id),
                )
                connection.execute(
                    """
                    INSERT INTO dead_letters(delivery_id, reason, dead_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(delivery_id) DO UPDATE SET
                        reason = excluded.reason, dead_at = excluded.dead_at
                    """,
                    (delivery_id, reason, now_iso),
                )

            endpoint_id = admission["endpoint_id"]
            successful_exchange = outcome is DispatchOutcome.SUCCEEDED
            self._record_circuit(
                connection,
                endpoint_id=endpoint_id,
                probe_token=admission["probe_token"],
                successful=successful_exchange,
                now=now,
            )
            connection.execute(
                """
                UPDATE dispatch_admissions
                SET state = 'committed', reason = ?, updated_at = ?
                WHERE delivery_id = ? AND lease_token_hash = ?
                """,
                (outcome.value, now_iso, delivery_id, _token_hash(lease_token)),
            )

    def release_stale(self, *, before: datetime, now: datetime) -> int:
        with self.database.write() as connection:
            result = connection.execute(
                """
                UPDATE dispatch_admissions
                SET state = 'released', reason = 'stale_admission', updated_at = ?
                WHERE state = 'admitted' AND updated_at <= ?
                  AND NOT EXISTS (
                    SELECT 1 FROM deliveries d
                    WHERE d.id = dispatch_admissions.delivery_id
                      AND d.state = 'in_flight'
                      AND d.lease_expires_at > ?
                  )
                """,
                (to_iso(now), to_iso(before), to_iso(now)),
            )
            return result.rowcount

    @staticmethod
    def _require_live(row, lease_token: str, now_iso: str) -> None:
        if row is None:
            raise NotFound("delivery not found")
        if (
            row["state"] != "in_flight"
            or row["lease_token"] != lease_token
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] < now_iso
        ):
            raise LeaseLost("delivery lease is missing, expired, or fenced")

    @staticmethod
    def _defer(
        connection,
        delivery_id: str,
        lease_token: str,
        *,
        next_attempt_at: datetime,
        reason: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE deliveries SET state = 'retry_wait', next_attempt_at = ?,
                last_error = ?, lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, version = version + 1
            WHERE id = ? AND lease_token = ? AND state = 'in_flight'
            """,
            (to_iso(next_attempt_at), reason, delivery_id, lease_token),
        )
        if updated.rowcount != 1:
            raise LeaseLost("delivery lease changed during admission")

    @staticmethod
    def _upsert_admission(
        connection,
        *,
        delivery_id: str,
        tenant_id: str,
        endpoint_id: str,
        token_hash: str,
        rate_reserved: float,
        probe_token: str | None,
        state: str,
        reason: str | None,
        now_iso: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO dispatch_admissions(
                delivery_id, tenant_id, endpoint_id, lease_token_hash,
                rate_reserved, probe_token, state, reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(delivery_id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                endpoint_id = excluded.endpoint_id,
                lease_token_hash = excluded.lease_token_hash,
                rate_reserved = excluded.rate_reserved,
                probe_token = excluded.probe_token,
                state = excluded.state,
                reason = excluded.reason,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                delivery_id,
                tenant_id,
                endpoint_id,
                token_hash,
                rate_reserved,
                probe_token,
                state,
                reason,
                now_iso,
                now_iso,
            ),
        )

    def _record_circuit(
        self,
        connection,
        *,
        endpoint_id: str,
        probe_token: str | None,
        successful: bool,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM endpoint_circuits WHERE endpoint_id = ?",
            (endpoint_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"circuit for endpoint {endpoint_id} not found")
        if row["state"] == "half_open" and row["probe_token"] != probe_token:
            raise LeaseLost("circuit probe is stale")
        if successful:
            connection.execute(
                """
                UPDATE endpoint_circuits SET state = 'closed', failure_count = 0,
                    opened_until = NULL, probe_token = NULL, updated_at = ?,
                    version = version + 1 WHERE endpoint_id = ?
                """,
                (to_iso(now), endpoint_id),
            )
            return
        failures = int(row["failure_count"]) + 1
        should_open = row["state"] == "half_open" or (
            failures >= self.circuit_failure_threshold
        )
        connection.execute(
            """
            UPDATE endpoint_circuits SET state = ?, failure_count = ?,
                opened_until = ?, probe_token = NULL, updated_at = ?,
                version = version + 1 WHERE endpoint_id = ?
            """,
            (
                "open" if should_open else "closed",
                failures,
                to_iso(now + timedelta(seconds=self.circuit_recovery_seconds))
                if should_open
                else None,
                to_iso(now),
                endpoint_id,
            ),
        )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
