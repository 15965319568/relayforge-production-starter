from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .control_db import ControlDatabase
from .timeutil import to_iso


class DiagnosticService:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def snapshot(
        self, *, now: datetime, stale_worker_seconds: int = 120
    ) -> dict[str, Any]:
        now_iso = to_iso(now)
        stale_iso = to_iso(now - timedelta(seconds=stale_worker_seconds))
        with self.database.connection() as connection:
            states = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state"
                )
            }
            tenants = [
                {
                    "tenant_id": row["tenant_id"],
                    "due": row["due"],
                    "active": row["active"],
                    "dead": row["dead"],
                    "oldest_due_at": row["oldest_due_at"],
                }
                for row in connection.execute(
                    """
                    SELECT event.tenant_id,
                           SUM(CASE WHEN delivery.state IN ('pending', 'retry_wait')
                                         AND delivery.next_attempt_at <= ?
                                    THEN 1 ELSE 0 END) AS due,
                           SUM(CASE WHEN delivery.state = 'in_flight'
                                         AND delivery.lease_expires_at > ?
                                    THEN 1 ELSE 0 END) AS active,
                           SUM(CASE WHEN delivery.state = 'dead' THEN 1 ELSE 0 END) AS dead,
                           MIN(CASE WHEN delivery.state IN ('pending', 'retry_wait')
                                         AND delivery.next_attempt_at <= ?
                                    THEN delivery.next_attempt_at END) AS oldest_due_at
                    FROM events event
                    JOIN deliveries delivery ON delivery.event_id = event.id
                    GROUP BY event.tenant_id
                    ORDER BY event.tenant_id
                    """,
                    (now_iso, now_iso, now_iso),
                )
            ]
            open_circuits = connection.execute(
                "SELECT COUNT(*) AS count FROM endpoint_circuits WHERE state != 'closed'"
            ).fetchone()["count"]
            stale_workers = connection.execute(
                "SELECT COUNT(*) AS count FROM worker_nodes WHERE last_seen_at <= ?",
                (stale_iso,),
            ).fetchone()["count"]
            replay_jobs = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM replay_jobs GROUP BY state"
                )
            }
            endpoints = [dict(row) for row in connection.execute(
                "SELECT id, url, secret FROM endpoints ORDER BY id"
            )]
            workers = [dict(row) for row in connection.execute(
                "SELECT worker_id, metadata_json FROM worker_nodes ORDER BY worker_id"
            )]
        return {
            "captured_at": now_iso,
            "deliveries": dict(sorted(states.items())),
            "tenants": tenants,
            "open_circuits": open_circuits,
            "stale_workers": stale_workers,
            "replay_jobs": dict(sorted(replay_jobs.items())),
            "endpoints": endpoints,
            "workers": workers,
        }
