from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from .control_db import ControlDatabase, new_control_id
from .control_models import Subscription
from .errors import NotFound, ValidationError
from .filters import compile_filter
from .timeutil import from_iso, to_iso
from .topics import topic_matches, validate_pattern, validate_topic


class SubscriptionRepository:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def create(
        self,
        *,
        tenant_id: str,
        endpoint_id: str,
        topic_pattern: str,
        filter_expression: Mapping[str, Any] | str | None,
        now: datetime,
        subscription_id: str | None = None,
    ) -> Subscription:
        tenant = tenant_id.strip()
        endpoint = endpoint_id.strip()
        if not tenant or not endpoint:
            raise ValidationError("tenant_id and endpoint_id are required")
        validate_pattern(topic_pattern)
        compiled = compile_filter(filter_expression)
        identifier = subscription_id or new_control_id("sub")
        timestamp = to_iso(now)
        with self.database.write() as connection:
            endpoint_row = connection.execute(
                "SELECT tenant_id FROM endpoints WHERE id = ? AND enabled = 1",
                (endpoint,),
            ).fetchone()
            if endpoint_row is None:
                raise NotFound(f"enabled endpoint {endpoint} not found")
            if endpoint_row["tenant_id"] != tenant:
                raise ValidationError("endpoint does not belong to tenant")
            connection.execute(
                """
                INSERT INTO subscriptions(
                    id, tenant_id, endpoint_id, topic_pattern, filter_json,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    identifier,
                    tenant,
                    endpoint,
                    topic_pattern.strip(),
                    compiled.to_json(),
                    timestamp,
                    timestamp,
                ),
            )
        return Subscription(
            id=identifier,
            tenant_id=tenant,
            endpoint_id=endpoint,
            topic_pattern=topic_pattern.strip(),
            filter_json=compiled.to_json(),
            enabled=True,
            created_at=now,
            updated_at=now,
        )

    def set_enabled(self, subscription_id: str, *, enabled: bool, now: datetime) -> None:
        with self.database.write() as connection:
            updated = connection.execute(
                "UPDATE subscriptions SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, to_iso(now), subscription_id),
            )
            if updated.rowcount != 1:
                raise NotFound(f"subscription {subscription_id} not found")

    def list_for_tenant(self, tenant_id: str) -> list[Subscription]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE tenant_id = ? ORDER BY created_at, id
                """,
                (tenant_id,),
            ).fetchall()
        return [_subscription(row) for row in rows]

    def route(self, *, tenant_id: str, topic: str, payload: Any) -> list[str]:
        validate_topic(topic)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT sub.*, endpoint.enabled
                FROM subscriptions sub
                JOIN endpoints endpoint ON endpoint.id = sub.endpoint_id
                WHERE sub.tenant_id = ? AND sub.enabled = 1
                ORDER BY sub.created_at, sub.id
                """,
                (tenant_id,),
            ).fetchall()
        endpoints: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not topic_matches(row["topic_pattern"], topic):
                continue
            if not compile_filter(row["filter_json"]).matches(payload):
                continue
            seen.add(row["endpoint_id"])
            endpoints.append(row["endpoint_id"])
        return endpoints


def _subscription(row) -> Subscription:
    return Subscription(
        id=row["id"],
        tenant_id=row["tenant_id"],
        endpoint_id=row["endpoint_id"],
        topic_pattern=row["topic_pattern"],
        filter_json=row["filter_json"],
        enabled=bool(row["enabled"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )
