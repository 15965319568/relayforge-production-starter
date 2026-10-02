from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from .control_schema import CONTROL_SCHEMA_VERSION
from .production_db import ProductionDatabase
from .production_models import HealthReport
from .production_schema import PRODUCTION_SCHEMA_VERSION
from .schema import SCHEMA_VERSION
from .timeutil import to_iso, utc_now


class HealthService:
    def __init__(
        self,
        database: ProductionDatabase,
        *,
        clock: Callable[[], datetime] = utc_now,
        stale_worker_seconds: int = 120,
    ) -> None:
        if stale_worker_seconds <= 0:
            raise ValueError("stale_worker_seconds must be positive")
        self.database = database
        self.clock = clock
        self.stale_worker_seconds = stale_worker_seconds

    def liveness(self) -> HealthReport:
        now = self.clock()
        try:
            with self.database.connection() as connection:
                value = connection.execute("SELECT 1").fetchone()[0]
            checks = {"sqlite_read": {"ok": value == 1}}
            status = "ok" if value == 1 else "failed"
        except sqlite3.Error as exc:
            checks = {"sqlite_read": {"ok": False, "error": type(exc).__name__}}
            status = "failed"
        return HealthReport(status, checks, now)

    def readiness(self) -> HealthReport:
        now = self.clock()
        checks: dict[str, dict[str, object]] = {}
        try:
            with self.database.write() as connection:
                connection.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS readiness_probe(value INTEGER)"
                )
                connection.execute("DELETE FROM readiness_probe")
                connection.execute("INSERT INTO readiness_probe(value) VALUES (1)")
                value = connection.execute(
                    "SELECT value FROM readiness_probe"
                ).fetchone()[0]
                checks["sqlite_write_read"] = {"ok": value == 1}
                versions = {
                    "delivery": connection.execute(
                        "SELECT version FROM schema_meta"
                    ).fetchone()[0],
                    "control": connection.execute(
                        "SELECT version FROM control_meta"
                    ).fetchone()[0],
                    "production": connection.execute(
                        "SELECT version FROM production_meta"
                    ).fetchone()[0],
                }
                expected = {
                    "delivery": SCHEMA_VERSION,
                    "control": CONTROL_SCHEMA_VERSION,
                    "production": PRODUCTION_SCHEMA_VERSION,
                }
                checks["schema_versions"] = {
                    "ok": versions == expected,
                    "versions": versions,
                }
                stale_cutoff = to_iso(
                    now - timedelta(seconds=self.stale_worker_seconds)
                )
                stale = connection.execute(
                    """
                    SELECT COUNT(*) FROM worker_nodes
                    WHERE draining = 0 AND last_seen_at < ?
                    """,
                    (stale_cutoff,),
                ).fetchone()[0]
                checks["workers"] = {"ok": stale == 0, "stale": stale}
                draft_migrations = connection.execute(
                    """
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type = 'table' AND name LIKE '%_migration_pending'
                    """
                ).fetchone()[0]
                checks["migrations"] = {
                    "ok": draft_migrations == 0,
                    "unfinished": draft_migrations,
                }
        except sqlite3.Error as exc:
            checks["sqlite_write_read"] = {
                "ok": False,
                "error": type(exc).__name__,
            }
        status = "ready" if checks and all(item["ok"] for item in checks.values()) else "not_ready"
        return HealthReport(status, checks, now)
