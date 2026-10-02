from __future__ import annotations

import sqlite3


PRODUCTION_SCHEMA_VERSION = 1

PRODUCTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS production_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_credentials (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    token_hash BLOB NOT NULL,
    salt BLOB NOT NULL,
    scopes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS dispatch_admissions (
    delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    lease_token_hash TEXT NOT NULL,
    rate_reserved REAL NOT NULL DEFAULT 0 CHECK (rate_reserved >= 0),
    probe_token TEXT,
    state TEXT NOT NULL CHECK (state IN ('admitted', 'committed', 'released')),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consistency_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    fingerprint TEXT,
    finding_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consistency_findings (
    run_id TEXT NOT NULL REFERENCES consistency_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS repair_plans (
    id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL REFERENCES consistency_runs(id),
    source_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'applied', 'stale', 'cancelled')),
    action_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS repair_actions (
    plan_id TEXT NOT NULL REFERENCES repair_plans(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    expected_version INTEGER,
    payload_json TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY (plan_id, ordinal)
);

CREATE TABLE IF NOT EXISTS backup_records (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    delivery_schema_version INTEGER NOT NULL,
    control_schema_version INTEGER NOT NULL,
    production_schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_credentials_tenant
    ON api_credentials (tenant_id, revoked_at, id);
CREATE INDEX IF NOT EXISTS ix_admissions_state
    ON dispatch_admissions (state, updated_at, delivery_id);
CREATE INDEX IF NOT EXISTS ix_findings_code
    ON consistency_findings (code, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_repair_state
    ON repair_plans (state, created_at, id);
"""


def apply_production_schema(connection: sqlite3.Connection) -> None:
    pending = ""
    for line in PRODUCTION_SCHEMA_SQL.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                connection.execute(statement)
            pending = ""
    if pending.strip():
        raise RuntimeError("incomplete production schema statement")

