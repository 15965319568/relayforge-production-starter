from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class AdmissionState(StrEnum):
    ADMITTED = "admitted"
    COMMITTED = "committed"
    RELEASED = "released"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RepairPlanState(StrEnum):
    DRAFT = "draft"
    APPLIED = "applied"
    STALE = "stale"
    CANCELLED = "cancelled"


class DispatchOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRIED = "retried"
    DEAD = "dead"
    DEFERRED = "deferred"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class Principal:
    credential_id: str
    tenant_id: str | None
    scopes: frozenset[str]

    @property
    def is_operator(self) -> bool:
        return self.tenant_id is None


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    id: str
    tenant_id: str | None
    scopes: frozenset[str]
    token: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    delivery_id: str
    allowed: bool
    reason: str | None
    retry_at: datetime | None
    probe_token: str | None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    delivery_id: str
    outcome: DispatchOutcome
    attempt_ordinal: int | None = None
    status_code: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead: int = 0
    deferred: int = 0
    lease_lost: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "retried": self.retried,
            "dead": self.dead,
            "deferred": self.deferred,
            "lease_lost": self.lease_lost,
        }


@dataclass(frozen=True, slots=True)
class ConsistencyFinding:
    code: str
    severity: FindingSeverity
    entity_type: str
    entity_id: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    run_id: str
    started_at: datetime
    completed_at: datetime
    findings: tuple[ConsistencyFinding, ...]


@dataclass(frozen=True, slots=True)
class RepairPlan:
    id: str
    source_run_id: str
    source_fingerprint: str
    state: RepairPlanState
    action_count: int
    created_at: datetime
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class RepairAction:
    ordinal: int
    action_type: str
    entity_type: str
    entity_id: str
    expected_version: int | None
    payload: Mapping[str, Any]
    applied_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BackupManifest:
    id: str
    path: str
    sha256: str
    size_bytes: int
    delivery_schema_version: int
    control_schema_version: int
    production_schema_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    count: int
    total: float
    minimum: float | None
    maximum: float | None
    buckets: tuple[tuple[float, int], ...]


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    checks: Mapping[str, Mapping[str, Any]]
    checked_at: datetime
