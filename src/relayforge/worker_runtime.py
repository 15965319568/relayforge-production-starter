from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime

from .config import ServiceConfig
from .delivery_processor import DeliveryProcessor
from .lease_supervisor import LeaseSupervisor
from .production_models import DispatchOutcome, DispatchResult, RuntimeSummary
from .scheduler import FairScheduler
from .shutdown import ShutdownController
from .store import Store
from .timeutil import utc_now
from .workers import WorkerRegistry


class WorkerRuntime:
    """Parallel worker with cooperative drain, heartbeats, and lease renewal."""

    def __init__(
        self,
        *,
        worker_id: str,
        store: Store,
        registry: WorkerRegistry,
        processor: DeliveryProcessor,
        config: ServiceConfig | None = None,
        scheduler: FairScheduler | None = None,
        concurrency: int = 8,
        renewal_interval_seconds: float | None = None,
        heartbeat_interval_seconds: float = 15.0,
        clock: Callable[[], datetime] = utc_now,
        shutdown: ShutdownController | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.worker_id = worker_id
        self.store = store
        self.registry = registry
        self.processor = processor
        self.config = config or ServiceConfig()
        self.scheduler = scheduler or FairScheduler()
        self.concurrency = concurrency
        self.clock = clock
        self.shutdown = shutdown or ShutdownController()
        self.renewal_interval_seconds = renewal_interval_seconds or max(
            0.1, self.config.lease_seconds / 3
        )
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.supervisor = LeaseSupervisor(
            store, lease_seconds=self.config.lease_seconds, clock=clock
        )
        self._generation: str | None = None

    @property
    def generation(self) -> str | None:
        return self._generation

    def start(self, *, metadata: dict[str, object] | None = None) -> str:
        registration = self.registry.register(
            self.worker_id,
            now=self.clock(),
            metadata={"concurrency": self.concurrency, **(metadata or {})},
        )
        self._generation = registration.generation
        return registration.generation

    def request_shutdown(self, *, timeout_seconds: float = 30.0) -> None:
        if self._generation is not None:
            self.registry.set_draining(
                self.worker_id,
                self._generation,
                draining=True,
                now=self.clock(),
            )

    def run_cycle(self, *, limit: int | None = None) -> RuntimeSummary:
        if self._generation is None:
            self.start()
        if self.shutdown.requested:
            return RuntimeSummary()
        batch_limit = min(
            limit or self.config.default_batch_size,
            self.concurrency,
        )
        if batch_limit <= 0:
            raise ValueError("limit must be positive")
        now = self.clock()
        self._heartbeat(now)
        candidates = self.store.list_due_candidates(now=now)
        selected = self.scheduler.select(
            candidates,
            limit=batch_limit,
            tenant_max_in_flight=self.config.tenant_max_in_flight,
        )
        claimed = self.store.claim_selected(
            selected,
            worker_id=self.worker_id,
            now=now,
            lease_seconds=self.config.lease_seconds,
            tenant_max_in_flight=self.config.tenant_max_in_flight,
        )
        if not claimed:
            return RuntimeSummary()
        for delivery in claimed:
            self.supervisor.track(delivery.delivery_id, delivery.lease_token)

        totals: Counter[str] = Counter()
        with ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix=f"relayforge-{self.worker_id}",
        ) as executor:
            futures: dict[Future[DispatchResult], str] = {
                executor.submit(self.processor.process, delivery): delivery.delivery_id
                for delivery in claimed
            }
            last_renewal = time.monotonic()
            last_heartbeat = last_renewal
            while futures:
                timeout = min(
                    self.renewal_interval_seconds,
                    self.heartbeat_interval_seconds,
                )
                done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
                for future in done:
                    delivery_id = futures.pop(future)
                    self.supervisor.untrack(delivery_id)
                    try:
                        result = future.result()
                    except Exception:
                        totals[DispatchOutcome.LEASE_LOST.value] += 1
                    else:
                        totals[result.outcome.value] += 1
                monotonic_now = time.monotonic()
                if (
                    futures
                    and monotonic_now - last_renewal
                    >= self.renewal_interval_seconds
                ):
                    lost = self.supervisor.renew_once(now=self.clock())
                    totals[DispatchOutcome.LEASE_LOST.value] += len(lost)
                    last_renewal = monotonic_now
                if (
                    futures
                    and monotonic_now - last_heartbeat
                    >= self.heartbeat_interval_seconds
                ):
                    self._heartbeat(self.clock())
                    last_heartbeat = monotonic_now
                if self.shutdown.deadline_reached:
                    for future, delivery_id in tuple(futures.items()):
                        if future.cancel():
                            futures.pop(future)
                            self.supervisor.untrack(delivery_id)
                    if futures:
                        break
        return RuntimeSummary(
            claimed=len(claimed),
            succeeded=totals[DispatchOutcome.SUCCEEDED.value],
            retried=totals[DispatchOutcome.RETRIED.value],
            dead=totals[DispatchOutcome.DEAD.value],
            deferred=totals[DispatchOutcome.DEFERRED.value],
            lease_lost=totals[DispatchOutcome.LEASE_LOST.value],
        )

    def run_forever(self, *, poll_seconds: float = 0.25) -> RuntimeSummary:
        if poll_seconds < 0:
            raise ValueError("poll_seconds cannot be negative")
        totals: Counter[str] = Counter()
        while not self.shutdown.requested:
            result = self.run_cycle()
            totals.update(result.as_dict())
            if result.claimed == 0 and self.shutdown.wait(poll_seconds):
                break
        return RuntimeSummary(**{key: totals[key] for key in RuntimeSummary().as_dict()})

    def _heartbeat(self, now: datetime) -> None:
        if self._generation is None:
            raise RuntimeError("worker is not registered")
        self.registry.heartbeat(self.worker_id, self._generation, now=now)
