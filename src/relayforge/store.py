from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .errors import (
    IdempotencyConflict,
    InvalidTransition,
    LeaseLost,
    NotFound,
    ValidationError,
)
from .models import ClaimedDelivery, DueCandidate, Endpoint, Event
from .redaction import safe_excerpt
from .migrations import migrate_v0_to_v1
from .schema import SCHEMA_VERSION, apply_schema
from .signing import canonical_json
from .timeutil import from_iso, to_iso, utc_now


class Store:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=max(0, self.busy_timeout_ms) / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {max(0, self.busy_timeout_ms)}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            meta_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_meta'
                """
            ).fetchone()
            if meta_exists is None:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    apply_schema(connection)
                    connection.execute(
                        "INSERT INTO schema_meta(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                return

            row = connection.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                raise RuntimeError("schema_meta is empty")
            if row["version"] == 0:
                migrate_v0_to_v1(connection, migrated_at=utc_now())
                return
            if row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported schema version {row['version']}; expected {SCHEMA_VERSION}"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                apply_schema(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def register_endpoint(
        self,
        *,
        tenant_id: str,
        url: str,
        secret: str,
        max_in_flight: int = 4,
        endpoint_id: str | None = None,
        now: datetime | None = None,
    ) -> Endpoint:
        tenant_id = _required("tenant_id", tenant_id)
        secret = _required("secret", secret)
        endpoint_id = endpoint_id or _new_id("ep")
        if max_in_flight <= 0:
            raise ValidationError("max_in_flight must be positive")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValidationError("endpoint URL must be absolute http(s)")
        created_at = now or utc_now()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO endpoints(
                    id, tenant_id, url, secret, max_in_flight, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    endpoint_id,
                    tenant_id,
                    url,
                    secret,
                    max_in_flight,
                    to_iso(created_at),
                ),
            )
        return Endpoint(
            id=endpoint_id,
            tenant_id=tenant_id,
            url=url,
            secret=secret,
            max_in_flight=max_in_flight,
            enabled=True,
            created_at=created_at,
        )

    def enqueue_event(
        self,
        *,
        tenant_id: str,
        topic: str,
        payload: Any,
        idempotency_key: str,
        event_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Event, bool]:
        tenant_id = _required("tenant_id", tenant_id)
        topic = _required("topic", topic)
        idempotency_key = _required("idempotency_key", idempotency_key)
        try:
            payload_json = canonical_json(payload).decode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"payload is not canonical JSON: {exc}") from exc
        accepted_at = now or utc_now()
        event_id = event_id or _new_id("evt")

        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM events
                WHERE tenant_id = ? AND idempotency_key = ?
                """,
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return _event(existing), False

            connection.execute(
                """
                INSERT INTO events(
                    id, tenant_id, topic, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    tenant_id,
                    topic,
                    payload_json,
                    idempotency_key,
                    to_iso(accepted_at),
                ),
            )
            endpoints = connection.execute(
                """
                SELECT id FROM endpoints
                WHERE tenant_id = ? AND enabled = 1
                ORDER BY id
                """,
                (tenant_id,),
            ).fetchall()
            for endpoint in endpoints:
                connection.execute(
                    """
                    INSERT INTO deliveries(
                        id, event_id, endpoint_id, state, next_attempt_at, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        _new_id("dlv"),
                        event_id,
                        endpoint["id"],
                        to_iso(accepted_at),
                        to_iso(accepted_at),
                    ),
                )
        return (
            Event(
                id=event_id,
                tenant_id=tenant_id,
                topic=topic,
                payload_json=payload_json,
                idempotency_key=idempotency_key,
                created_at=accepted_at,
            ),
            True,
        )

    def list_due_candidates(self, *, now: datetime) -> list[DueCandidate]:
        now_iso = to_iso(now)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    e.tenant_id,
                    d.endpoint_id,
                    CASE
                        WHEN d.state = 'in_flight' THEN d.lease_expires_at
                        ELSE d.next_attempt_at
                    END AS due_at,
                    d.created_at,
                    ep.max_in_flight AS endpoint_max_in_flight,
                    (
                        SELECT COUNT(*) FROM deliveries active
                        WHERE active.endpoint_id = d.endpoint_id
                          AND active.state = 'in_flight'
                          AND active.lease_expires_at > ?
                    ) AS endpoint_in_flight,
                    (
                        SELECT COUNT(*)
                        FROM deliveries tenant_active
                        JOIN events tenant_event ON tenant_event.id = tenant_active.event_id
                        WHERE tenant_event.tenant_id = e.tenant_id
                          AND tenant_active.state = 'in_flight'
                          AND tenant_active.lease_expires_at > ?
                    ) AS tenant_in_flight
                FROM deliveries d
                JOIN events e ON e.id = d.event_id
                JOIN endpoints ep ON ep.id = d.endpoint_id
                WHERE ep.enabled = 1
                  AND (
                    (d.state IN ('pending', 'retry_wait') AND d.next_attempt_at <= ?)
                    OR
                    (d.state = 'in_flight' AND d.lease_expires_at <= ?)
                  )
                ORDER BY due_at, d.created_at, d.id
                """,
                (now_iso, now_iso, now_iso, now_iso),
            ).fetchall()
        return [
            DueCandidate(
                delivery_id=row["delivery_id"],
                tenant_id=row["tenant_id"],
                endpoint_id=row["endpoint_id"],
                due_at=from_iso(row["due_at"]),
                created_at=from_iso(row["created_at"]),
                endpoint_max_in_flight=row["endpoint_max_in_flight"],
                endpoint_in_flight=row["endpoint_in_flight"],
                tenant_in_flight=row["tenant_in_flight"],
            )
            for row in rows
        ]

    def claim_selected(
        self,
        delivery_ids: Iterable[str],
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        tenant_max_in_flight: int | None = None,
    ) -> list[ClaimedDelivery]:
        worker_id = _required("worker_id", worker_id)
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        if tenant_max_in_flight is not None and tenant_max_in_flight <= 0:
            raise ValidationError("tenant_max_in_flight must be positive")
        ordered_ids = list(dict.fromkeys(delivery_ids))
        if not ordered_ids:
            return []
        claimed: list[ClaimedDelivery] = []
        now_iso = to_iso(now)
        expires_at = now + timedelta(seconds=lease_seconds)
        expires_iso = to_iso(expires_at)
        with self._write() as connection:
            for delivery_id in ordered_ids:
                row = connection.execute(
                    """
                    SELECT d.*, e.tenant_id, e.topic, e.payload_json,
                           ep.url AS endpoint_url, ep.secret AS endpoint_secret,
                           ep.enabled
                    FROM deliveries d
                    JOIN events e ON e.id = d.event_id
                    JOIN endpoints ep ON ep.id = d.endpoint_id
                    WHERE d.id = ?
                    """,
                    (delivery_id,),
                ).fetchone()
                if row is None or not row["enabled"]:
                    continue
                due = (
                    row["state"] in {"pending", "retry_wait"}
                    and row["next_attempt_at"] <= now_iso
                ) or (
                    row["state"] == "in_flight"
                    and row["lease_expires_at"] is not None
                    and row["lease_expires_at"] < now_iso
                )
                if not due:
                    continue
                token = uuid.uuid4().hex
                updated = connection.execute(
                    """
                    UPDATE deliveries
                    SET state = 'in_flight', lease_owner = ?, lease_token = ?,
                        lease_expires_at = ?, version = version + 1
                    WHERE id = ? AND (
                        (state IN ('pending', 'retry_wait') AND next_attempt_at <= ?)
                        OR (state = 'in_flight' AND lease_expires_at < ?)
                    )
                    """,
                    (
                        worker_id,
                        token,
                        expires_iso,
                        delivery_id,
                        now_iso,
                        now_iso,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                claimed.append(
                    ClaimedDelivery(
                        delivery_id=delivery_id,
                        event_id=row["event_id"],
                        endpoint_id=row["endpoint_id"],
                        tenant_id=row["tenant_id"],
                        topic=row["topic"],
                        payload_json=row["payload_json"],
                        endpoint_url=row["endpoint_url"],
                        endpoint_secret=row["endpoint_secret"],
                        attempt_count=row["attempt_count"],
                        first_attempt_at=(
                            from_iso(row["first_attempt_at"])
                            if row["first_attempt_at"]
                            else None
                        ),
                        lease_token=token,
                        lease_expires_at=expires_at,
                    )
                )
        return claimed

    def start_attempt(
        self, delivery_id: str, lease_token: str, *, now: datetime
    ) -> int:
        now_iso = to_iso(now)
        with self._write() as connection:
            row = self._require_live_lease(
                connection, delivery_id, lease_token, now_iso=now_iso
            )
            existing = connection.execute(
                """
                SELECT ordinal FROM attempts
                WHERE delivery_id = ? AND lease_token = ?
                """,
                (delivery_id, lease_token),
            ).fetchone()
            ordinal = row["attempt_count"] + 1
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

    def renew_lease(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> datetime:
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_iso = to_iso(now)
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._write() as connection:
            self._require_live_lease(
                connection, delivery_id, lease_token, now_iso=now_iso
            )
            connection.execute(
                """
                UPDATE deliveries
                SET lease_expires_at = ?, version = version + 1
                WHERE id = ? AND lease_token = ?
                """,
                (to_iso(expires_at), delivery_id, lease_token),
            )
        return expires_at

    def finalize_success(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        now: datetime,
        status_code: int,
        request_id: str | None,
        response_body: bytes,
    ) -> None:
        now_iso = to_iso(now)
        with self._write() as connection:
            self._require_live_lease(
                connection, delivery_id, lease_token, now_iso=now_iso
            )
            self._finish_attempt(
                connection,
                delivery_id,
                lease_token,
                finished_at=now_iso,
                outcome="succeeded",
                status_code=status_code,
                error_kind=None,
                retry_after=None,
                request_id=request_id,
                response_body=response_body,
            )
            connection.execute(
                """
                UPDATE deliveries
                SET state = 'succeeded', completed_at = ?, last_status = ?,
                    last_error = NULL, lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, version = version + 1
                WHERE id = ?
                """,
                (now_iso, status_code, delivery_id),
            )

    def finalize_retry(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        now: datetime,
        next_attempt_at: datetime,
        error_kind: str,
        status_code: int | None,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
    ) -> None:
        now_iso = to_iso(now)
        with self._write() as connection:
            self._require_live_lease(
                connection, delivery_id, lease_token, now_iso=now_iso
            )
            self._finish_attempt(
                connection,
                delivery_id,
                lease_token,
                finished_at=now_iso,
                outcome="retryable",
                status_code=status_code,
                error_kind=error_kind,
                retry_after=retry_after,
                request_id=request_id,
                response_body=response_body,
            )
            connection.execute(
                """
                UPDATE deliveries
                SET state = 'retry_wait', next_attempt_at = ?, last_error = ?,
                    last_status = ?, completed_at = NULL, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    to_iso(next_attempt_at),
                    error_kind,
                    status_code,
                    delivery_id,
                ),
            )

    def finalize_dead(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        now: datetime,
        reason: str,
        outcome: str,
        status_code: int | None,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
    ) -> None:
        if outcome not in {"terminal", "retryable"}:
            raise ValidationError("dead attempt outcome must be terminal or retryable")
        now_iso = to_iso(now)
        with self._write() as connection:
            self._require_live_lease(
                connection, delivery_id, lease_token, now_iso=now_iso
            )
            self._finish_attempt(
                connection,
                delivery_id,
                lease_token,
                finished_at=now_iso,
                outcome=outcome,
                status_code=status_code,
                error_kind=reason,
                retry_after=retry_after,
                request_id=request_id,
                response_body=response_body,
            )
            connection.execute(
                """
                UPDATE deliveries
                SET state = 'dead', completed_at = ?, last_error = ?,
                    last_status = ?, lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, version = version + 1
                WHERE id = ?
                """,
                (now_iso, reason, status_code, delivery_id),
            )
            connection.execute(
                """
                INSERT INTO dead_letters(delivery_id, reason, dead_at)
                VALUES (?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    reason = excluded.reason,
                    dead_at = excluded.dead_at
                """,
                (delivery_id, reason, now_iso),
            )

    def inspect_delivery(self, delivery_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT d.*, e.tenant_id, e.topic, e.id AS event_id,
                       ep.id AS endpoint_id
                FROM deliveries d
                JOIN events e ON e.id = d.event_id
                JOIN endpoints ep ON ep.id = d.endpoint_id
                WHERE d.id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"delivery {delivery_id} not found")
            attempts = connection.execute(
                """
                SELECT ordinal, started_at, finished_at, outcome, status_code,
                       error_kind, retry_after, request_id, response_excerpt
                FROM attempts WHERE delivery_id = ? ORDER BY ordinal
                """,
                (delivery_id,),
            ).fetchall()
        delivery = {
            key: row[key]
            for key in (
                "id",
                "event_id",
                "endpoint_id",
                "tenant_id",
                "topic",
                "state",
                "attempt_count",
                "first_attempt_at",
                "next_attempt_at",
                "lease_owner",
                "lease_expires_at",
                "last_error",
                "last_status",
                "created_at",
                "completed_at",
            )
        }
        delivery["lease_token"] = row["lease_token"]
        delivery["attempts"] = [dict(attempt) for attempt in attempts]
        return delivery

    def list_dead_letters(
        self, *, tenant_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValidationError("limit must be positive")
        query = """
            SELECT dl.delivery_id, e.tenant_id, dl.reason, dl.dead_at,
                   dl.replay_count, dl.last_replayed_at
            FROM dead_letters dl
            JOIN deliveries d ON d.id = dl.delivery_id
            JOIN events e ON e.id = d.event_id
            WHERE d.state = 'dead'
        """
        params: list[Any] = []
        if tenant_id is not None:
            query += " AND e.tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY dl.dead_at, dl.delivery_id LIMIT ?"
        params.append(limit)
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def replay_dead_letters(
        self,
        *,
        now: datetime,
        tenant_id: str | None = None,
        reason: str | None = None,
        before: datetime | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> list[str]:
        if limit <= 0:
            raise ValidationError("limit must be positive")
        query = """
            SELECT dl.delivery_id
            FROM dead_letters dl
            JOIN deliveries d ON d.id = dl.delivery_id
            JOIN events e ON e.id = d.event_id
            WHERE d.state = 'dead'
        """
        params: list[Any] = []
        if tenant_id is not None:
            query += " AND e.tenant_id = ?"
            params.append(tenant_id)
        if reason is not None:
            query += " AND dl.reason = ?"
            params.append(reason)
        if before is not None:
            query += " AND dl.dead_at < ?"
            params.append(to_iso(before))
        query += " ORDER BY dl.dead_at, dl.delivery_id LIMIT ?"
        params.append(limit)
        now_iso = to_iso(now)
        with self._write() as connection:
            ids = [
                row["delivery_id"]
                for row in connection.execute(query, params).fetchall()
            ]
            if dry_run:
                return ids
            for delivery_id in ids:
                connection.execute(
                    """
                    UPDATE deliveries
                    SET state = 'pending', next_attempt_at = ?, completed_at = NULL,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error = NULL,
                        last_status = NULL, version = version + 1
                    WHERE id = ? AND state = 'dead'
                    """,
                    (now_iso, delivery_id),
                )
                connection.execute(
                    """
                    UPDATE dead_letters
                    SET replay_count = replay_count + 1, last_replayed_at = ?
                    WHERE delivery_id = ?
                    """,
                    (now_iso, delivery_id),
                )
            return ids

    def append_audit(
        self,
        *,
        occurred_at: datetime,
        kind: str,
        delivery_id: str | None,
        details: dict[str, Any],
    ) -> None:
        encoded = json.dumps(
            details, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO audit_log(occurred_at, kind, delivery_id, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (to_iso(occurred_at), kind, delivery_id, encoded),
            )

    def list_audit(self, delivery_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT occurred_at, kind, delivery_id, details_json
                FROM audit_log WHERE delivery_id = ? ORDER BY id
                """,
                (delivery_id,),
            ).fetchall()
        return [
            {
                "occurred_at": row["occurred_at"],
                "kind": row["kind"],
                "delivery_id": row["delivery_id"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _require_live_lease(
        connection: sqlite3.Connection,
        delivery_id: str,
        lease_token: str,
        *,
        now_iso: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"delivery {delivery_id} not found")
        if row["state"] != "in_flight":
            raise LeaseLost(f"lease is no longer valid for delivery {delivery_id}")
        return row

    @staticmethod
    def _finish_attempt(
        connection: sqlite3.Connection,
        delivery_id: str,
        lease_token: str,
        *,
        finished_at: str,
        outcome: str,
        status_code: int | None,
        error_kind: str | None,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE attempts
            SET finished_at = ?, outcome = ?, status_code = ?, error_kind = ?,
                retry_after = ?, request_id = ?, response_excerpt = ?
            WHERE delivery_id = ? AND lease_token = ? AND outcome = 'started'
            """,
            (
                finished_at,
                outcome,
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
            raise InvalidTransition("active attempt is missing or already finalized")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _required(name: str, value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValidationError(f"{name} is required")
    return candidate


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        tenant_id=row["tenant_id"],
        topic=row["topic"],
        payload_json=row["payload_json"],
        idempotency_key=row["idempotency_key"],
        created_at=from_iso(row["created_at"]),
    )
