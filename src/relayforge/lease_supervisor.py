from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from .errors import LeaseLost
from .store import Store
from .timeutil import utc_now


class LeaseSupervisor:
    """Thread-safe registry and renewal loop for active delivery leases."""

    def __init__(
        self,
        store: Store,
        *,
        lease_seconds: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.lease_seconds = lease_seconds
        self.clock = clock
        self._leases: dict[str, str] = {}
        self._lost: set[str] = set()
        self._lock = threading.Lock()

    def track(self, delivery_id: str, lease_token: str) -> None:
        with self._lock:
            self._leases[delivery_id] = lease_token
            self._lost.discard(delivery_id)

    def untrack(self, delivery_id: str) -> None:
        with self._lock:
            self._leases.pop(delivery_id, None)

    def is_lost(self, delivery_id: str) -> bool:
        with self._lock:
            return delivery_id in self._lost

    def active_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._leases))

    def renew_once(self, *, now: datetime | None = None) -> tuple[str, ...]:
        renewed_at = now or self.clock()
        with self._lock:
            leases = tuple(self._leases.items())
        lost: list[str] = []
        for delivery_id, token in leases:
            try:
                self.store.renew_lease(
                    delivery_id,
                    token,
                    now=renewed_at,
                    lease_seconds=self.lease_seconds,
                )
            except LeaseLost:
                lost.append(delivery_id)
                with self._lock:
                    if self._leases.get(delivery_id) == token:
                        self._leases.pop(delivery_id, None)
                        self._lost.add(delivery_id)
        return tuple(sorted(lost))
