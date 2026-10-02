from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class DeliveryState(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class AttemptOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: str
    tenant_id: str
    url: str
    secret: str
    max_in_flight: int
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    tenant_id: str
    topic: str
    payload_json: str
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DueCandidate:
    delivery_id: str
    tenant_id: str
    endpoint_id: str
    due_at: datetime
    created_at: datetime
    endpoint_max_in_flight: int
    endpoint_in_flight: int
    tenant_in_flight: int


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    delivery_id: str
    event_id: str
    endpoint_id: str
    tenant_id: str
    topic: str
    payload_json: str
    endpoint_url: str
    endpoint_secret: str
    attempt_count: int
    first_attempt_at: datetime | None
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class Attempt:
    delivery_id: str
    ordinal: int
    lease_token: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    status_code: int | None
    error_kind: str | None
    retry_after: str | None
    request_id: str | None
    response_excerpt: str | None


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class RunSummary:
    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead: int = 0
    lease_lost: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "retried": self.retried,
            "dead": self.dead,
            "lease_lost": self.lease_lost,
        }

