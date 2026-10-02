from __future__ import annotations

import sqlite3


CONTROL_SCHEMA_VERSION = 2

CONTROL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS control_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    topic_pattern TEXT NOT NULL,
    filter_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (endpoint_id, topic_pattern, filter_json)
);

CREATE TABLE IF NOT EXISTS endpoint_secret_versions (
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    version INTEGER NOT NULL CHECK (version > 0),
    key_id TEXT NOT NULL,
    secret TEXT NOT NULL,
    active_from TEXT NOT NULL,
    retired_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (endpoint_id, version),
    UNIQUE (endpoint_id, key_id)
);

CREATE TABLE IF NOT EXISTS tenant_rate_policies (
    tenant_id TEXT PRIMARY KEY,
    rate_per_second REAL NOT NULL CHECK (rate_per_second > 0),
    burst REAL NOT NULL CHECK (burst >= 1),
    max_in_flight INTEGER NOT NULL CHECK (max_in_flight > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_bucket_state (
    tenant_id TEXT PRIMARY KEY REFERENCES tenant_rate_policies(tenant_id),
    tokens REAL NOT NULL CHECK (tokens >= 0),
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS endpoint_circuits (
    endpoint_id TEXT PRIMARY KEY REFERENCES endpoints(id),
    state TEXT NOT NULL CHECK (state IN ('closed', 'open', 'half_open')),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    opened_until TEXT,
    probe_token TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    tenant_id TEXT,
    reason TEXT,
    before_at TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'running', 'completed', 'cancelled', 'failed')
    ),
    total_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_job_items (
    job_id TEXT NOT NULL REFERENCES replay_jobs(id) ON DELETE CASCADE,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id),
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'skipped')),
    processed_at TEXT,
    PRIMARY KEY (job_id, delivery_id),
    UNIQUE (job_id, ordinal)
);

CREATE TABLE IF NOT EXISTS worker_nodes (
    worker_id TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    draining INTEGER NOT NULL DEFAULT 0 CHECK (draining IN (0, 1)),
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retention_runs (
    id TEXT PRIMARY KEY,
    response_before TEXT NOT NULL,
    audit_before TEXT NOT NULL,
    response_excerpts_cleared INTEGER NOT NULL,
    audit_rows_deleted INTEGER NOT NULL,
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_subscriptions_route
    ON subscriptions (tenant_id, enabled, topic_pattern, id);
CREATE INDEX IF NOT EXISTS ix_secret_versions_active
    ON endpoint_secret_versions (endpoint_id, active_from, retired_at, version);
CREATE INDEX IF NOT EXISTS ix_replay_jobs_state
    ON replay_jobs (state, created_at, id);
CREATE INDEX IF NOT EXISTS ix_replay_items_pending
    ON replay_job_items (job_id, state, ordinal);
CREATE INDEX IF NOT EXISTS ix_workers_seen
    ON worker_nodes (last_seen_at, worker_id);
CREATE INDEX IF NOT EXISTS ix_audit_page
    ON audit_log (occurred_at, id);
"""


def apply_control_schema(connection: sqlite3.Connection) -> None:
    pending = ""
    for line in CONTROL_SCHEMA_SQL.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.execute(statement)
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete control schema statement")

