from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .schema import SCHEMA_VERSION, apply_schema
from .timeutil import to_iso


LEGACY_TABLES = (
    "schema_meta",
    "endpoints",
    "events",
    "deliveries",
    "attempt_log",
    "dead_letter",
)

STATE_MAP = {
    "queued": "pending",
    "processing": "pending",
    "retrying": "retry_wait",
    "delivered": "succeeded",
    "failed": "dead",
}

OUTCOME_MAP = {
    "success": "succeeded",
    "retry": "retryable",
    "failure": "terminal",
    "started": "retryable",
}


def migrate_v0_to_v1(
    connection: sqlite3.Connection, *, migrated_at: datetime
) -> None:
    """Atomically migrate the incident-era schema while preserving history."""
    existing = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = set(LEGACY_TABLES) - existing
    if missing:
        raise RuntimeError(f"legacy v0 database is missing tables: {sorted(missing)}")

    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in LEGACY_TABLES:
            connection.execute(f'ALTER TABLE "{table}" RENAME TO "legacy_{table}"')
        apply_schema(connection)

        endpoint_rows = connection.execute(
            "SELECT * FROM legacy_endpoints ORDER BY id"
        ).fetchall()
        for row in endpoint_rows:
            connection.execute(
                """
                INSERT INTO endpoints(
                    id, tenant_id, url, secret, max_in_flight, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["tenant_id"],
                    row["url"],
                    row["secret"],
                    row["max_in_flight"],
                    1 if row["enabled"] else 0,
                    _timestamp(row["created_at"]),
                ),
            )

        event_rows = connection.execute(
            "SELECT * FROM legacy_events ORDER BY id"
        ).fetchall()
        for row in event_rows:
            connection.execute(
                """
                INSERT INTO events(
                    id, tenant_id, topic, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["tenant_id"],
                    row["topic"],
                    _canonicalize_payload(row["payload_json"]),
                    row["idempotency_key"],
                    _timestamp(row["created_at"]),
                ),
            )

        delivery_rows = connection.execute(
            "SELECT * FROM legacy_deliveries ORDER BY id"
        ).fetchall()
        for row in delivery_rows:
            try:
                state = STATE_MAP[row["status"]]
            except KeyError as exc:
                raise RuntimeError(
                    f"unsupported legacy delivery status: {row['status']}"
                ) from exc
            next_at = row["available_at"]
            if row["status"] == "processing" and row["locked_until"]:
                next_at = row["locked_until"]
            connection.execute(
                """
                INSERT INTO deliveries(
                    id, event_id, endpoint_id, state, attempt_count,
                    first_attempt_at, next_attempt_at, lease_owner, lease_token,
                    lease_expires_at, last_error, last_status, version,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 0, ?, ?)
                """,
                (
                    row["id"],
                    row["event_id"],
                    row["endpoint_id"],
                    state,
                    row["attempts"],
                    _timestamp(row["first_attempt_at"]),
                    _timestamp(next_at),
                    row["last_error"],
                    row["last_status"],
                    _timestamp(row["created_at"]),
                    _timestamp(row["finished_at"]),
                ),
            )

        attempt_rows = connection.execute(
            "SELECT * FROM legacy_attempt_log ORDER BY delivery_id, attempt_no"
        ).fetchall()
        for row in attempt_rows:
            try:
                outcome = OUTCOME_MAP[row["result"]]
            except KeyError as exc:
                raise RuntimeError(
                    f"unsupported legacy attempt result: {row['result']}"
                ) from exc
            finished_at = row["finished_at"]
            error_kind = row["error"]
            if outcome == "abandoned":
                finished_at = finished_at or row["started_at"]
                error_kind = error_kind or "migration_recovered"
            connection.execute(
                """
                INSERT INTO attempts(
                    delivery_id, ordinal, lease_token, started_at, finished_at,
                    outcome, status_code, error_kind, retry_after, request_id,
                    response_excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    row["delivery_id"],
                    row["attempt_no"],
                    f"legacy:{row['delivery_id']}:{row['attempt_no']}",
                    _timestamp(row["started_at"]),
                    _timestamp(finished_at),
                    outcome,
                    row["status"],
                    error_kind,
                    _excerpt(row["response"]),
                ),
            )

        legacy_dead = {
            row["delivery_id"]: row
            for row in connection.execute(
                "SELECT * FROM legacy_dead_letter ORDER BY delivery_id"
            ).fetchall()
        }
        for row in delivery_rows:
            if STATE_MAP[row["status"]] != "dead":
                continue
            dead = legacy_dead.get(row["id"])
            connection.execute(
                """
                INSERT INTO dead_letters(
                    delivery_id, reason, dead_at, replay_count, last_replayed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    dead["reason"] if dead else (row["last_error"] or "legacy_failed"),
                    _timestamp(dead["created_at"] if dead else row["finished_at"]),
                    dead["replay_count"] if dead else 0,
                    _timestamp(dead["last_replayed_at"]) if dead else None,
                ),
            )

        connection.execute(
            "INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,)
        )
        details = json.dumps(
            {
                "from": 0,
                "to": SCHEMA_VERSION,
                "endpoints": len(endpoint_rows),
                "events": len(event_rows),
                "deliveries": len(delivery_rows),
                "attempts": len(attempt_rows),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO audit_log(occurred_at, kind, delivery_id, details_json)
            VALUES (?, 'schema_migration', NULL, ?)
            """,
            (to_iso(migrated_at), details),
        )

        for table in reversed(LEGACY_TABLES):
            connection.execute(f'DROP TABLE "legacy_{table}"')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return to_iso(parsed)


def _canonicalize_payload(value: str) -> str:
    parsed: Any = json.loads(value)
    return json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    )


def _excerpt(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = "".join(
        char if (char in "\t\n" or ord(char) >= 32) else "?" for char in value
    )
    return cleaned[:256]
