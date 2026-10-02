from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShutdownSnapshot:
    requested: bool
    deadline_monotonic: float | None
    remaining_seconds: float | None


class ShutdownController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._deadline: float | None = None
        self._lock = threading.Lock()

    def request(self, *, timeout_seconds: float) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")
        candidate = time.monotonic() + timeout_seconds
        with self._lock:
            self._deadline = (
                candidate
                if self._deadline is None
                else min(self._deadline, candidate)
            )
            self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def deadline_reached(self) -> bool:
        with self._lock:
            deadline = self._deadline
        return deadline is not None and time.monotonic() >= deadline

    def wait(self, seconds: float) -> bool:
        return self._event.wait(max(0.0, seconds))

    def snapshot(self) -> ShutdownSnapshot:
        with self._lock:
            deadline = self._deadline
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        return ShutdownSnapshot(self.requested, deadline, remaining)
