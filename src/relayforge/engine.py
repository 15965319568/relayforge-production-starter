from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from .config import ServiceConfig
from .errors import LeaseLost, TransportError
from .metrics import DeliveryMetrics
from .models import (
    ClaimedDelivery,
    OutboundRequest,
    RunSummary,
    TransportResponse,
)
from .redaction import redact_url, safe_excerpt, sanitize_headers
from .scheduler import FairScheduler
from .signing import build_headers, canonical_json
from .store import Store
from .timeutil import utc_now
from .transport import Transport


class DeliveryEngine:
    def __init__(
        self,
        *,
        store: Store,
        transport: Transport,
        config: ServiceConfig | None = None,
        scheduler: FairScheduler | None = None,
        metrics: DeliveryMetrics | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.transport = transport
        self.config = config or ServiceConfig()
        self.scheduler = scheduler or FairScheduler()
        self.metrics = metrics or DeliveryMetrics()
        self.clock = clock

    def run_once(self, *, worker_id: str, limit: int | None = None) -> RunSummary:
        batch_limit = limit if limit is not None else self.config.default_batch_size
        if batch_limit <= 0:
            raise ValueError("limit must be positive")
        claim_time = self.clock()
        candidates = self.store.list_due_candidates(now=claim_time)
        selected = self.scheduler.select(
            candidates,
            limit=batch_limit,
            tenant_max_in_flight=self.config.tenant_max_in_flight,
        )
        deliveries = self.store.claim_selected(
            selected,
            worker_id=worker_id,
            now=claim_time,
            lease_seconds=self.config.lease_seconds,
        )
        totals = {
            "claimed": len(deliveries),
            "succeeded": 0,
            "retried": 0,
            "dead": 0,
            "lease_lost": 0,
        }
        self.metrics.increment("claimed", len(deliveries))
        for delivery in deliveries:
            outcome = self._deliver(delivery)
            totals[outcome] += 1
            self.metrics.increment(outcome)
        return RunSummary(**totals)

    def _deliver(self, delivery: ClaimedDelivery) -> str:
        started_at = self.clock()
        try:
            ordinal = self.store.start_attempt(
                delivery.delivery_id, delivery.lease_token, now=started_at
            )
        except LeaseLost:
            return "lease_lost"

        payload = json.loads(delivery.payload_json)
        body = canonical_json(payload)
        timestamp = int(started_at.timestamp())
        headers = build_headers(
            secret=delivery.endpoint_secret,
            timestamp=timestamp,
            event_id=delivery.event_id,
            delivery_id=delivery.delivery_id,
            body=body,
        )
        request = OutboundRequest(
            method="POST",
            url=delivery.endpoint_url,
            headers=headers,
            body=body,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        self.store.append_audit(
            occurred_at=started_at,
            kind="request",
            delivery_id=delivery.delivery_id,
            details={
                "attempt": ordinal,
                "method": request.method,
                "url": redact_url(request.url),
                "headers": sanitize_headers(request.headers),
                "body_bytes": len(request.body),
            },
        )

        try:
            response = self.transport.send(request)
        except TransportError as exc:
            finished_at = self.clock()
            error_kind = f"transport:{exc}"
            self.store.append_audit(
                occurred_at=finished_at,
                kind="response",
                delivery_id=delivery.delivery_id,
                details={"attempt": ordinal, "error": error_kind},
            )
            return self._retry_or_dead(
                delivery=delivery,
                ordinal=ordinal,
                started_at=started_at,
                finished_at=finished_at,
                error_kind=error_kind,
                status_code=None,
                retry_after=None,
                request_id=None,
                response_body=b"",
            )

        finished_at = self.clock()
        retry_after = _header(response.headers, "retry-after")
        request_id = _header(response.headers, "x-request-id")
        self.store.append_audit(
            occurred_at=finished_at,
            kind="response",
            delivery_id=delivery.delivery_id,
            details={
                "attempt": ordinal,
                "status": response.status_code,
                "headers": sanitize_headers(response.headers),
                "body_excerpt": safe_excerpt(response.body),
            },
        )

        if 200 <= response.status_code <= 299:
            try:
                self.store.finalize_success(
                    delivery.delivery_id,
                    delivery.lease_token,
                    now=finished_at,
                    status_code=response.status_code,
                    request_id=request_id,
                    response_body=response.body,
                )
            except LeaseLost:
                return "lease_lost"
            return "succeeded"

        if _is_retryable(response.status_code):
            return self._retry_or_dead(
                delivery=delivery,
                ordinal=ordinal,
                started_at=started_at,
                finished_at=finished_at,
                error_kind=f"http:{response.status_code}",
                status_code=response.status_code,
                retry_after=retry_after,
                request_id=request_id,
                response_body=response.body,
            )

        try:
            self.store.finalize_dead(
                delivery.delivery_id,
                delivery.lease_token,
                now=finished_at,
                reason=f"http:{response.status_code}",
                outcome="terminal",
                status_code=response.status_code,
                retry_after=retry_after,
                request_id=request_id,
                response_body=response.body,
            )
        except LeaseLost:
            return "lease_lost"
        return "dead"

    def _retry_or_dead(
        self,
        *,
        delivery: ClaimedDelivery,
        ordinal: int,
        started_at: datetime,
        finished_at: datetime,
        error_kind: str,
        status_code: int | None,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
    ) -> str:
        delay = self.config.retry.delay_seconds(
            delivery.delivery_id,
            ordinal,
            now=finished_at,
            retry_after=retry_after,
        )
        next_attempt_at = finished_at + timedelta(seconds=delay)
        first_attempt_at = delivery.first_attempt_at or started_at
        permitted = self.config.retry.permits_retry(
            attempt_ordinal=ordinal,
            first_attempt_at=first_attempt_at,
            scheduled_at=next_attempt_at,
        )
        try:
            if permitted:
                self.store.finalize_retry(
                    delivery.delivery_id,
                    delivery.lease_token,
                    now=finished_at,
                    next_attempt_at=next_attempt_at,
                    error_kind=error_kind,
                    status_code=status_code,
                    retry_after=retry_after,
                    request_id=request_id,
                    response_body=response_body,
                )
                return "retried"
            reason = (
                "attempts_exhausted"
                if ordinal >= self.config.retry.max_attempts
                else "retry_window_exhausted"
            )
            self.store.finalize_dead(
                delivery.delivery_id,
                delivery.lease_token,
                now=finished_at,
                reason=reason,
                outcome="retryable",
                status_code=status_code,
                retry_after=retry_after,
                request_id=request_id,
                response_body=response_body,
            )
            return "dead"
        except LeaseLost:
            return "lease_lost"


def _is_retryable(status_code: int) -> bool:
    return 500 <= status_code <= 599


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return headers.get(name)
