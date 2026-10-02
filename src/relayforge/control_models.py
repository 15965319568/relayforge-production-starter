from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ReplayJobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Subscription:
    id: str
    tenant_id: str
    endpoint_id: str
    topic_pattern: str
    filter_json: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SecretVersion:
    endpoint_id: str
    version: int
    key_id: str
    secret: str
    active_from: datetime
    retired_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RatePolicy:
    tenant_id: str
    rate_per_second: float
    burst: float
    max_in_flight: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    remaining: float
    retry_after_seconds: float


@dataclass(frozen=True, slots=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState
    probe_token: str | None
    retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReplayJob:
    id: str
    idempotency_key: str
    request_hash: str
    tenant_id: str | None
    reason: str | None
    before_at: datetime | None
    state: ReplayJobState
    total_count: int
    processed_count: int
    skipped_count: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReplayBatchResult:
    job_id: str
    applied: int
    skipped: int
    remaining: int
    state: ReplayJobState


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    worker_id: str
    generation: str
    last_seen_at: datetime
    draining: bool
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: tuple[Mapping[str, Any], ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    response_excerpts: int
    audit_rows: int


@dataclass(frozen=True, slots=True)
class RetentionResult:
    response_excerpts_cleared: int
    audit_rows_deleted: int
    dry_run: bool

