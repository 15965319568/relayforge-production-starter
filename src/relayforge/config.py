from __future__ import annotations

from dataclasses import dataclass, field

from .retry import RetryPolicy


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    lease_seconds: int = 30
    request_timeout_seconds: float = 10.0
    default_batch_size: int = 25
    tenant_max_in_flight: int = 10
    busy_timeout_ms: int = 5_000
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy())

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.default_batch_size <= 0:
            raise ValueError("default_batch_size must be positive")
        if self.tenant_max_in_flight <= 0:
            raise ValueError("tenant_max_in_flight must be positive")
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")

