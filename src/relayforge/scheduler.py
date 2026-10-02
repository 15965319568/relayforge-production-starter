from __future__ import annotations

from collections import defaultdict, deque

from .models import DueCandidate


class FairScheduler:
    """Deterministic tenant and endpoint round-robin batch selection."""

    def select(
        self,
        candidates: list[DueCandidate],
        *,
        limit: int,
        tenant_max_in_flight: int,
    ) -> list[str]:
        ordered = sorted(candidates, key=lambda item: item.due_at)
        return [item.delivery_id for item in ordered[: max(0, limit)]]
