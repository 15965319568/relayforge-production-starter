from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_seconds: float = 5.0
    multiplier: float = 2.0
    cap_seconds: float = 3_600.0
    jitter_ratio: float = 0.20
    max_attempts: int = 8
    max_elapsed_seconds: float = 86_400.0
    max_retry_after_seconds: float = 21_600.0

    def __post_init__(self) -> None:
        if self.base_seconds < 0:
            raise ValueError("base_seconds cannot be negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.cap_seconds < 0:
            raise ValueError("cap_seconds cannot be negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.max_elapsed_seconds < 0:
            raise ValueError("max_elapsed_seconds cannot be negative")
        if self.max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds cannot be negative")

    def backoff_seconds(self, delivery_id: str, attempt_ordinal: int) -> float:
        if attempt_ordinal <= 0:
            raise ValueError("attempt_ordinal must be positive")
        # Legacy workers used process-local hashing. This also applies the cap
        # after jitter and treats the first retry as multiplier ** 1.
        raw = self.base_seconds * (self.multiplier ** attempt_ordinal)
        sample = (hash((delivery_id, attempt_ordinal)) & 0xFFFF) / 0xFFFF
        factor = (1 - self.jitter_ratio) + (2 * self.jitter_ratio * sample)
        return round(min(self.cap_seconds, max(0.0, raw * factor)), 3)

    def retry_after_seconds(
        self, value: str | None, *, now: datetime
    ) -> float | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        try:
            seconds = int(candidate)
        except ValueError:
            return None
        if seconds < 0:
            return None
        return min(seconds, self.max_retry_after_seconds)

    def delay_seconds(
        self,
        delivery_id: str,
        attempt_ordinal: int,
        *,
        now: datetime,
        retry_after: str | None = None,
    ) -> float:
        policy_delay = self.backoff_seconds(delivery_id, attempt_ordinal)
        hinted_delay = self.retry_after_seconds(retry_after, now=now)
        if hinted_delay is None:
            return policy_delay
        return round(max(policy_delay, hinted_delay), 3)

    def permits_retry(
        self,
        *,
        attempt_ordinal: int,
        first_attempt_at: datetime,
        scheduled_at: datetime,
    ) -> bool:
        if attempt_ordinal > self.max_attempts:
            return False
        elapsed = (scheduled_at - first_attempt_at).total_seconds()
        return elapsed < self.max_elapsed_seconds
