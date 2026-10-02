from __future__ import annotations

import unittest
from datetime import timedelta

from _support import BASE_TIME
from relayforge.models import DueCandidate
from relayforge.scheduler import FairScheduler


def candidate(
    delivery: str,
    tenant: str,
    endpoint: str,
    *,
    offset: int = 0,
    endpoint_limit: int = 10,
    endpoint_active: int = 0,
    tenant_active: int = 0,
) -> DueCandidate:
    return DueCandidate(
        delivery_id=delivery,
        tenant_id=tenant,
        endpoint_id=endpoint,
        due_at=BASE_TIME + timedelta(seconds=offset),
        created_at=BASE_TIME,
        endpoint_max_in_flight=endpoint_limit,
        endpoint_in_flight=endpoint_active,
        tenant_in_flight=tenant_active,
    )


class FairSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = FairScheduler()

    def test_round_robins_tenants_even_when_first_has_old_backlog(self) -> None:
        rows = [
            candidate("a1", "a", "a-ep", offset=0),
            candidate("a2", "a", "a-ep", offset=1),
            candidate("a3", "a", "a-ep", offset=2),
            candidate("b1", "b", "b-ep", offset=3),
            candidate("b2", "b", "b-ep", offset=4),
        ]
        self.assertEqual(
            self.scheduler.select(rows, limit=4, tenant_max_in_flight=10),
            ["a1", "b1", "a2", "b2"],
        )

    def test_round_robins_endpoints_within_tenant(self) -> None:
        rows = [
            candidate("a1", "a", "ep-1", offset=0),
            candidate("a2", "a", "ep-1", offset=1),
            candidate("a3", "a", "ep-2", offset=2),
            candidate("a4", "a", "ep-2", offset=3),
        ]
        self.assertEqual(
            self.scheduler.select(rows, limit=4, tenant_max_in_flight=10),
            ["a1", "a3", "a2", "a4"],
        )

    def test_respects_endpoint_remaining_capacity(self) -> None:
        rows = [
            candidate("a1", "a", "ep", endpoint_limit=2, endpoint_active=1),
            candidate("a2", "a", "ep", endpoint_limit=2, endpoint_active=1),
        ]
        self.assertEqual(
            self.scheduler.select(rows, limit=2, tenant_max_in_flight=10), ["a1"]
        )

    def test_respects_tenant_remaining_capacity(self) -> None:
        rows = [
            candidate("a1", "a", "ep-1", tenant_active=2),
            candidate("a2", "a", "ep-2", tenant_active=2),
        ]
        self.assertEqual(
            self.scheduler.select(rows, limit=2, tenant_max_in_flight=3), ["a1"]
        )

    def test_selection_is_independent_of_input_order(self) -> None:
        rows = [
            candidate("b", "b", "ep-b", offset=1),
            candidate("a", "a", "ep-a", offset=0),
        ]
        forward = self.scheduler.select(rows, limit=2, tenant_max_in_flight=10)
        reverse = self.scheduler.select(list(reversed(rows)), limit=2, tenant_max_in_flight=10)
        self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()

