from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .audit_export import AuditExporter
from .circuit_breaker import PersistentCircuitBreaker
from .control_db import ControlDatabase
from .cursors import CursorCodec
from .errors import IdempotencyConflict, ValidationError
from .rate_limit import PersistentTokenBucket
from .replay_jobs import ReplayJobService
from .retention import RetentionService
from .secrets import SecretRing
from .subscriptions import SubscriptionRepository
from .timeutil import to_iso
from .workers import WorkerRegistry
from .auth import CredentialService
from .backup import BackupService
from .consistency import ConsistencyScanner
from .dispatch_repository import DispatchRepository
from .health import HealthService
from .production_db import ProductionDatabase
from .repair_executor import RepairExecutor
from .repair_plans import RepairPlanService
from .telemetry import production_metrics


class RelayForgePlatform:
    """Composition root for the enterprise control-plane services."""

    def __init__(
        self,
        database_path: str,
        *,
        cursor_secret: str,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.database = ProductionDatabase(
            database_path, busy_timeout_ms=busy_timeout_ms
        )
        self.subscriptions = SubscriptionRepository(self.database)
        self.secrets = SecretRing(self.database)
        self.rate_limits = PersistentTokenBucket(self.database)
        self.circuits = PersistentCircuitBreaker(self.database)
        self.replays = ReplayJobService(self.database)
        self.retention = RetentionService(self.database)
        self.workers = WorkerRegistry(self.database)
        self.audit = AuditExporter(
            self.database, cursor_codec=CursorCodec(cursor_secret)
        )
        self.credentials = CredentialService(self.database)
        self.dispatch = DispatchRepository(self.database)
        self.consistency = ConsistencyScanner(self.database)
        self.repair_plans = RepairPlanService(self.database)
        self.repairs = RepairExecutor(self.database)
        self.backups = BackupService(self.database)
        self.health = HealthService(self.database)
        self.metrics = production_metrics()

    def initialize(self, *, now: datetime) -> None:
        self.database.initialize(now=now)

    def ingest_routed_event(
        self,
        *,
        tenant_id: str,
        topic: str,
        payload: Any,
        idempotency_key: str,
        now: datetime,
        event_id: str | None = None,
    ) -> tuple[str, bool, tuple[str, ...]]:
        endpoints = self.subscriptions.route(
            tenant_id=tenant_id, topic=topic, payload=payload
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identifier = event_id or f"evt_{uuid.uuid4().hex}"
        now_iso = to_iso(now)
        with self.database.write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM events
                WHERE tenant_id = ? AND idempotency_key = ?
                """,
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["topic"] != topic or existing["payload_json"] != encoded:
                    raise IdempotencyConflict(
                        "idempotency key already belongs to different event content"
                    )
                routed = tuple(
                    row["endpoint_id"]
                    for row in connection.execute(
                        """
                        SELECT endpoint_id FROM deliveries
                        WHERE event_id = ? ORDER BY endpoint_id
                        """,
                        (existing["id"],),
                    ).fetchall()
                )
                return existing["id"], False, routed
            connection.execute(
                """
                INSERT INTO events(
                    id, tenant_id, topic, payload_json, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (identifier, tenant_id, topic, encoded, idempotency_key, now_iso),
            )
            for endpoint_id in endpoints:
                connection.execute(
                    """
                    INSERT INTO deliveries(
                        id, event_id, endpoint_id, state, next_attempt_at, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        f"dlv_{uuid.uuid4().hex}",
                        identifier,
                        endpoint_id,
                        now_iso,
                        now_iso,
                    ),
                )
        return identifier, True, tuple(endpoints)
