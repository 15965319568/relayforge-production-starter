from __future__ import annotations

import math
from datetime import datetime

from .control_db import ControlDatabase
from .control_models import RateDecision, RatePolicy
from .errors import NotFound, ValidationError
from .timeutil import from_iso, to_iso


class PersistentTokenBucket:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def configure(
        self,
        tenant_id: str,
        *,
        rate_per_second: float,
        burst: float,
        max_in_flight: int,
        now: datetime,
    ) -> RatePolicy:
        if not tenant_id.strip():
            raise ValidationError("tenant_id is required")
        if not math.isfinite(rate_per_second) or rate_per_second <= 0:
            raise ValidationError("rate_per_second must be finite and positive")
        if not math.isfinite(burst) or burst < 1:
            raise ValidationError("burst must be finite and at least 1")
        if max_in_flight <= 0:
            raise ValidationError("max_in_flight must be positive")
        now_iso = to_iso(now)
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO tenant_rate_policies(
                    tenant_id, rate_per_second, burst, max_in_flight, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    rate_per_second = excluded.rate_per_second,
                    burst = excluded.burst,
                    max_in_flight = excluded.max_in_flight,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, rate_per_second, burst, max_in_flight, now_iso),
            )
            current = connection.execute(
                "SELECT tokens FROM tenant_bucket_state WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            tokens = min(float(current["tokens"]), burst) if current else burst
            connection.execute(
                """
                INSERT INTO tenant_bucket_state(tenant_id, tokens, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    tokens = excluded.tokens, updated_at = excluded.updated_at,
                    version = tenant_bucket_state.version + 1
                """,
                (tenant_id, tokens, now_iso),
            )
        return RatePolicy(
            tenant_id=tenant_id,
            rate_per_second=rate_per_second,
            burst=burst,
            max_in_flight=max_in_flight,
            updated_at=now,
        )

    def policy(self, tenant_id: str) -> RatePolicy:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_rate_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if row is None:
            raise NotFound(f"rate policy for tenant {tenant_id} not found")
        return RatePolicy(
            tenant_id=row["tenant_id"],
            rate_per_second=row["rate_per_second"],
            burst=row["burst"],
            max_in_flight=row["max_in_flight"],
            updated_at=from_iso(row["updated_at"]),
        )

    def reserve(
        self, tenant_id: str, *, amount: float = 1.0, now: datetime
    ) -> RateDecision:
        if not math.isfinite(amount) or amount <= 0:
            raise ValidationError("amount must be finite and positive")
        now_iso = to_iso(now)
        with self.database.connection() as connection:
            policy = connection.execute(
                "SELECT * FROM tenant_rate_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            state = connection.execute(
                "SELECT * FROM tenant_bucket_state WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            if policy is None or state is None:
                raise NotFound(f"rate policy for tenant {tenant_id} not found")
            elapsed = abs((now - from_iso(state["updated_at"])).total_seconds())
            available = min(
                float(policy["burst"]),
                float(state["tokens"]) + elapsed * float(policy["rate_per_second"]),
            )
            allowed = available + 1e-12 >= amount
            remaining = available - amount if allowed else available
            retry_after = (
                0.0
                if allowed
                else max(0.0, (amount - available) / float(policy["rate_per_second"]))
            )
            connection.execute(
                """
                UPDATE tenant_bucket_state
                SET tokens = ?, updated_at = ?, version = version + 1
                WHERE tenant_id = ?
                """,
                (max(0.0, remaining), now_iso, tenant_id),
            )
        return RateDecision(
            allowed=allowed,
            remaining=round(max(0.0, remaining), 6),
            retry_after_seconds=round(retry_after, 6),
        )
