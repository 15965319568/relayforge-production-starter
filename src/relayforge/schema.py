from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoints (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    max_in_flight INTEGER NOT NULL CHECK (max_in_flight > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'in_flight', 'retry_wait', 'succeeded', 'dead')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    first_attempt_at TEXT,
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    last_status INTEGER,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (event_id, endpoint_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    lease_token TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('started', 'succeeded', 'retryable', 'terminal', 'abandoned')
    ),
    status_code INTEGER,
    error_kind TEXT,
    retry_after TEXT,
    request_id TEXT,
    response_excerpt TEXT,
    UNIQUE (delivery_id, ordinal),
    UNIQUE (delivery_id, lease_token)
);

CREATE TABLE IF NOT EXISTS dead_letters (
    delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),
    reason TEXT NOT NULL,
    dead_at TEXT NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0,
    last_replayed_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    delivery_id TEXT,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_deliveries_due
    ON deliveries (state, next_attempt_at, created_at, id);
CREATE INDEX IF NOT EXISTS ix_deliveries_lease
    ON deliveries (state, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_endpoints_tenant
    ON endpoints (tenant_id, enabled, id);
CREATE INDEX IF NOT EXISTS ix_attempts_delivery
    ON attempts (delivery_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_dead_letters_order
    ON dead_letters (dead_at, delivery_id);
"""


def apply_schema(connection: sqlite3.Connection) -> None:
    pending = ""
    for line in SCHEMA_SQL.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.execute(statement)
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete schema statement")
