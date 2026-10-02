from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

from .control_db import ControlDatabase, canonical_details
from .control_models import WorkerRegistration
from .errors import LeaseLost, NotFound, ValidationError
from .store import Store
from .timeutil import from_iso, to_iso


class WorkerRegistry:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def register(
        self,
        worker_id: str,
        *,
        now: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkerRegistration:
        identifier = worker_id.strip()
        if not identifier:
            raise ValidationError("worker_id is required")
        generation = uuid.uuid4().hex
        encoded = canonical_details(dict(metadata or {}))
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO worker_nodes(
                    worker_id, generation, last_seen_at, draining, metadata_json
                ) VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    generation = excluded.generation,
                    last_seen_at = excluded.last_seen_at,
                    draining = 0,
                    metadata_json = excluded.metadata_json
                """,
                (identifier, generation, to_iso(now), encoded),
            )
        return WorkerRegistration(identifier, generation, now, False, metadata or {})

    def heartbeat(
        self, worker_id: str, generation: str, *, now: datetime
    ) -> WorkerRegistration:
        with self.database.write() as connection:
            updated = connection.execute(
                """
                UPDATE worker_nodes SET last_seen_at = ?
                WHERE worker_id = ? AND generation = ?
                """,
                (to_iso(now), worker_id, generation),
            )
            if updated.rowcount != 1:
                raise LeaseLost(f"worker generation is stale for {worker_id}")
            row = connection.execute(
                "SELECT * FROM worker_nodes WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        return _worker(row)

    def set_draining(
        self, worker_id: str, generation: str, *, draining: bool, now: datetime
    ) -> None:
        with self.database.write() as connection:
            updated = connection.execute(
                """
                UPDATE worker_nodes
                SET draining = ?, last_seen_at = ?
                WHERE worker_id = ? AND generation = ?
                """,
                (1 if draining else 0, to_iso(now), worker_id, generation),
            )
            if updated.rowcount != 1:
                raise LeaseLost(f"worker generation is stale for {worker_id}")

    def stale(self, *, now: datetime, stale_after_seconds: int) -> list[WorkerRegistration]:
        if stale_after_seconds <= 0:
            raise ValidationError("stale_after_seconds must be positive")
        cutoff = to_iso(now - timedelta(seconds=stale_after_seconds))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM worker_nodes
                WHERE last_seen_at <= ? ORDER BY last_seen_at, worker_id
                """,
                (cutoff,),
            ).fetchall()
        return [_worker(row) for row in rows]


class LeaseRenewalCoordinator:
    def __init__(self, store: Store, *, lease_seconds: int) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.lease_seconds = lease_seconds
        self._leases: dict[str, str] = {}

    def track(self, delivery_id: str, lease_token: str) -> None:
        self._leases[delivery_id] = lease_token

    def untrack(self, delivery_id: str) -> None:
        self._leases.pop(delivery_id, None)

    def renew_all(self, *, now: datetime) -> tuple[str, ...]:
        lost: list[str] = []
        for delivery_id, token in list(self._leases.items()):
            try:
                self.store.renew_lease(
                    delivery_id,
                    token,
                    now=now,
                    lease_seconds=self.lease_seconds,
                )
            except LeaseLost:
                lost.append(delivery_id)
                self._leases.pop(delivery_id, None)
        return tuple(lost)


def _worker(row) -> WorkerRegistration:
    return WorkerRegistration(
        worker_id=row["worker_id"],
        generation=row["generation"],
        last_seen_at=from_iso(row["last_seen_at"]),
        draining=bool(row["draining"]),
        metadata=json.loads(row["metadata_json"]),
    )
