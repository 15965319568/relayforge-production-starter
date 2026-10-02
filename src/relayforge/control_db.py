from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .control_schema import CONTROL_SCHEMA_VERSION, apply_control_schema
from .errors import ValidationError
from .store import Store
from .timeutil import to_iso, utc_now


class ControlDatabase:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = str(path)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
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
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            self._begin_immediate(connection)
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self, *, now: datetime | None = None) -> None:
        Store(self.path, busy_timeout_ms=self.busy_timeout_ms).initialize()
        initialized_at = now or utc_now()
        with self.write() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_meta'"
            ).fetchone()
            if exists is None:
                apply_control_schema(connection)
                connection.execute(
                    "INSERT INTO control_meta(version) VALUES (?)",
                    (CONTROL_SCHEMA_VERSION,),
                )
            else:
                row = connection.execute("SELECT version FROM control_meta").fetchone()
                if row is None:
                    raise RuntimeError("control_meta is empty")
                if row["version"] == 1:
                    self._migrate_v1(connection, now=initialized_at)
                elif row["version"] != CONTROL_SCHEMA_VERSION:
                    raise RuntimeError(
                        f"unsupported control schema version {row['version']}"
                    )
                apply_control_schema(connection)
            self._backfill(connection, now=initialized_at)

    def _migrate_v1(self, connection: sqlite3.Connection, *, now: datetime) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(subscriptions)")
        }
        required = {"id", "tenant_id", "endpoint_id", "topic_pattern", "enabled", "created_at"}
        if not required.issubset(columns):
            raise RuntimeError("control v1 subscriptions table is incompatible")
        connection.execute("ALTER TABLE subscriptions RENAME TO legacy_control_subscriptions")
        connection.execute("DROP TABLE control_meta")
        apply_control_schema(connection)
        connection.execute(
            "INSERT INTO control_meta(version) VALUES (?)", (CONTROL_SCHEMA_VERSION,)
        )
        rows = connection.execute(
            "SELECT * FROM legacy_control_subscriptions ORDER BY id"
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO subscriptions(
                    id, tenant_id, endpoint_id, topic_pattern, filter_json,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', ?, ?, ?)
                """,
                (
                    row["id"],
                    row["tenant_id"],
                    row["endpoint_id"],
                    row["topic_pattern"],
                    row["enabled"],
                    row["created_at"],
                    to_iso(now),
                ),
            )
        connection.execute("DROP TABLE legacy_control_subscriptions")

    def _backfill(self, connection: sqlite3.Connection, *, now: datetime) -> None:
        now_iso = to_iso(now)
        endpoints = connection.execute(
            "SELECT id, tenant_id, secret, created_at FROM endpoints ORDER BY id"
        ).fetchall()
        for endpoint in endpoints:
            connection.execute(
                """
                INSERT OR IGNORE INTO endpoint_secret_versions(
                    endpoint_id, version, key_id, secret, active_from, created_at
                ) VALUES (?, 1, 'v1', ?, ?, ?)
                """,
                (
                    endpoint["id"],
                    endpoint["secret"],
                    endpoint["created_at"],
                    endpoint["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO subscriptions(
                    id, tenant_id, endpoint_id, topic_pattern, filter_json,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, '#', '{}', 1, ?, ?)
                """,
                (
                    f"sub_default_{endpoint['id']}",
                    endpoint["tenant_id"],
                    endpoint["id"],
                    now_iso,
                    now_iso,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO endpoint_circuits(
                    endpoint_id, state, failure_count, updated_at
                ) VALUES (?, 'closed', 0, ?)
                """,
                (endpoint["id"], now_iso),
            )
        tenants = {
            row["tenant_id"]
            for row in connection.execute("SELECT DISTINCT tenant_id FROM endpoints")
        }
        for tenant in sorted(tenants):
            connection.execute(
                """
                INSERT OR IGNORE INTO tenant_rate_policies(
                    tenant_id, rate_per_second, burst, max_in_flight, updated_at
                ) VALUES (?, 10.0, 20.0, 10, ?)
                """,
                (tenant, now_iso),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO tenant_bucket_state(
                    tenant_id, tokens, updated_at
                ) VALUES (?, 20.0, ?)
                """,
                (tenant, now_iso),
            )

    def _begin_immediate(self, connection: sqlite3.Connection) -> None:
        deadline = time.monotonic() + max(0, self.busy_timeout_ms) / 1_000
        delay = 0.001
        while True:
            try:
                connection.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                text = str(exc).casefold()
                if not ("locked" in text or "busy" in text) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.05)


def new_control_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_details(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def require_positive(name: str, value: float) -> float:
    if value <= 0:
        raise ValidationError(f"{name} must be positive")
    return value

