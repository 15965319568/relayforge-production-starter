from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from _enterprise_support import EnterpriseCase
from _support import BASE_TIME, seed_delivery
from relayforge.circuit_breaker import PersistentCircuitBreaker
from relayforge.control_models import CircuitState
from relayforge.errors import InvalidTransition, LeaseLost
from relayforge.workers import LeaseRenewalCoordinator, WorkerRegistry


class CircuitBreakerTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.circuit = PersistentCircuitBreaker(
            self.database, failure_threshold=2, recovery_seconds=10
        )

    def test_closed_circuit_allows_requests(self) -> None:
        decision = self.circuit.allow("ep-a", now=BASE_TIME)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.state, CircuitState.CLOSED)

    def test_failures_open_and_cooldown_blocks(self) -> None:
        self.assertEqual(
            self.circuit.record_failure("ep-a", now=BASE_TIME),
            CircuitState.CLOSED,
        )
        self.assertEqual(
            self.circuit.record_failure("ep-a", now=BASE_TIME), CircuitState.OPEN
        )
        decision = self.circuit.allow(
            "ep-a", now=BASE_TIME + timedelta(seconds=9)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.retry_at, BASE_TIME + timedelta(seconds=10))

    def test_exact_cooldown_boundary_allows_one_probe(self) -> None:
        for _ in range(2):
            self.circuit.record_failure("ep-a", now=BASE_TIME)
        first = self.circuit.allow("ep-a", now=BASE_TIME + timedelta(seconds=10))
        second = self.circuit.allow("ep-a", now=BASE_TIME + timedelta(seconds=10))
        self.assertTrue(first.allowed)
        self.assertIsNotNone(first.probe_token)
        self.assertFalse(second.allowed)

    def test_concurrent_half_open_allows_single_probe(self) -> None:
        for _ in range(2):
            self.circuit.record_failure("ep-a", now=BASE_TIME)
        barrier = threading.Barrier(5)

        def allow(_):
            barrier.wait()
            return PersistentCircuitBreaker(
                self.database, failure_threshold=2, recovery_seconds=10
            ).allow("ep-a", now=BASE_TIME + timedelta(seconds=10)).allowed

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(allow, range(5)))
        self.assertEqual(results.count(True), 1)

    def test_probe_success_closes_and_stale_probe_is_rejected(self) -> None:
        for _ in range(2):
            self.circuit.record_failure("ep-a", now=BASE_TIME)
        probe = self.circuit.allow("ep-a", now=BASE_TIME + timedelta(seconds=10))
        with self.assertRaises(InvalidTransition):
            self.circuit.record_success(
                "ep-a", now=BASE_TIME + timedelta(seconds=11), probe_token="wrong"
            )
        self.circuit.record_success(
            "ep-a", now=BASE_TIME + timedelta(seconds=11), probe_token=probe.probe_token
        )
        self.assertEqual(
            self.circuit.allow("ep-a", now=BASE_TIME + timedelta(seconds=11)).state,
            CircuitState.CLOSED,
        )

    def test_failed_probe_reopens_full_cooldown(self) -> None:
        for _ in range(2):
            self.circuit.record_failure("ep-a", now=BASE_TIME)
        probe = self.circuit.allow("ep-a", now=BASE_TIME + timedelta(seconds=10))
        state = self.circuit.record_failure(
            "ep-a", now=BASE_TIME + timedelta(seconds=11), probe_token=probe.probe_token
        )
        self.assertEqual(state, CircuitState.OPEN)
        self.assertFalse(
            self.circuit.allow("ep-a", now=BASE_TIME + timedelta(seconds=20)).allowed
        )


class WorkerRegistryTests(EnterpriseCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = WorkerRegistry(self.database)

    def test_register_and_heartbeat_preserve_metadata(self) -> None:
        worker = self.registry.register(
            "worker-1", now=BASE_TIME, metadata={"zone": "cn-east-1", "slots": 4}
        )
        heartbeat = self.registry.heartbeat(
            worker.worker_id,
            worker.generation,
            now=BASE_TIME + timedelta(seconds=5),
        )
        self.assertEqual(heartbeat.metadata["zone"], "cn-east-1")
        self.assertEqual(heartbeat.last_seen_at, BASE_TIME + timedelta(seconds=5))

    def test_reregister_fences_old_generation(self) -> None:
        old = self.registry.register("worker-1", now=BASE_TIME)
        new = self.registry.register(
            "worker-1", now=BASE_TIME + timedelta(seconds=1)
        )
        self.assertNotEqual(old.generation, new.generation)
        with self.assertRaises(LeaseLost):
            self.registry.heartbeat(
                old.worker_id,
                old.generation,
                now=BASE_TIME + timedelta(seconds=2),
            )

    def test_draining_requires_current_generation(self) -> None:
        worker = self.registry.register("worker-1", now=BASE_TIME)
        self.registry.set_draining(
            worker.worker_id,
            worker.generation,
            draining=True,
            now=BASE_TIME + timedelta(seconds=1),
        )
        current = self.registry.heartbeat(
            worker.worker_id,
            worker.generation,
            now=BASE_TIME + timedelta(seconds=2),
        )
        self.assertTrue(current.draining)

    def test_stale_boundary_is_inclusive_and_ordered(self) -> None:
        first = self.registry.register("worker-a", now=BASE_TIME)
        second = self.registry.register(
            "worker-b", now=BASE_TIME + timedelta(seconds=1)
        )
        stale = self.registry.stale(
            now=BASE_TIME + timedelta(seconds=10), stale_after_seconds=9
        )
        self.assertEqual([worker.worker_id for worker in stale], ["worker-a", "worker-b"])

    def test_lease_renewal_coordinator_removes_lost_lease(self) -> None:
        delivery = seed_delivery(
            self.store,
            tenant="org-b",
            endpoint_id="ep-extra",
            event_id="evt-extra",
            key="key-extra",
            now=BASE_TIME,
        )
        claim = self.store.claim_selected(
            [delivery], worker_id="worker", now=BASE_TIME, lease_seconds=5
        )[0]
        coordinator = LeaseRenewalCoordinator(self.store, lease_seconds=10)
        coordinator.track(delivery, claim.lease_token)
        self.assertEqual(
            coordinator.renew_all(now=BASE_TIME + timedelta(seconds=4)), ()
        )
        lost = coordinator.renew_all(now=BASE_TIME + timedelta(seconds=14))
        self.assertEqual(lost, (delivery,))


if __name__ == "__main__":
    unittest.main()

