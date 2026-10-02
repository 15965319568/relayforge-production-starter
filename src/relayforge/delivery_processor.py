from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from .config import ServiceConfig
from .dispatch_repository import DispatchRepository
from .errors import LeaseLost, TransportError
from .models import ClaimedDelivery, OutboundRequest
from .production_models import DispatchOutcome, DispatchResult
from .redaction import redact_url, safe_excerpt, sanitize_headers
from .retry import RetryPolicy
from .secrets import SecretRing
from .signing import build_headers, canonical_json
from .store import Store
from .timeutil import utc_now
from .transport import Transport


class DeliveryProcessor:
    """Runs one admitted HTTP exchange and commits its state atomically."""

    def __init__(
        self,
        *,
        repository: DispatchRepository,
        secret_ring: SecretRing,
        store: Store,
        transport: Transport,
        config: ServiceConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.secret_ring = secret_ring
        self.store = store
        self.transport = transport
        self.config = config or ServiceConfig()
        self.clock = clock

    def process(self, delivery: ClaimedDelivery) -> DispatchResult:
        admitted_at = self.clock()
        try:
            admission = self.repository.admit(
                delivery.delivery_id, delivery.lease_token, now=admitted_at
            )
        except LeaseLost:
            return DispatchResult(delivery.delivery_id, DispatchOutcome.LEASE_LOST)
        if not admission.allowed:
            self._audit(
                admitted_at,
                "dispatch_deferred",
                delivery.delivery_id,
                {"reason": admission.reason, "retry_at": _iso(admission.retry_at)},
            )
            return DispatchResult(
                delivery.delivery_id,
                DispatchOutcome.DEFERRED,
                reason=admission.reason,
            )

        started_at = self.clock()
        try:
            ordinal = self.repository.start_attempt(
                delivery.delivery_id, delivery.lease_token, now=started_at
            )
        except LeaseLost:
            return DispatchResult(delivery.delivery_id, DispatchOutcome.LEASE_LOST)
        body = canonical_json(json.loads(delivery.payload_json))
        headers = build_headers(
            secret=delivery.endpoint_secret,
            timestamp=int(started_at.timestamp()),
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
        self._audit(
            started_at,
            "request",
            delivery.delivery_id,
            {
                "attempt": ordinal,
                "method": "POST",
                "url": redact_url(request.url),
                "headers": sanitize_headers(request.headers),
                "body_bytes": len(body),
            },
        )
        try:
            response = self.transport.send(request)
        except TransportError as exc:
            finished_at = self.clock()
            error_kind = f"transport:{type(exc).__name__}"
            self._audit(
                finished_at,
                "response",
                delivery.delivery_id,
                {"attempt": ordinal, "error": error_kind},
            )
            return self._finish_failure(
                delivery,
                ordinal=ordinal,
                started_at=started_at,
                finished_at=finished_at,
                status_code=None,
                error_kind=error_kind,
                retry_after=None,
                request_id=None,
                response_body=b"",
            )

        finished_at = self.clock()
        retry_after = _header(response.headers, "retry-after")
        request_id = _header(response.headers, "x-request-id")
        self._audit(
            finished_at,
            "response",
            delivery.delivery_id,
            {
                "attempt": ordinal,
                "status": response.status_code,
                "headers": sanitize_headers(response.headers),
                "body_excerpt": safe_excerpt(response.body),
            },
        )
        if 200 <= response.status_code <= 299:
            return self._commit(
                delivery,
                DispatchOutcome.SUCCEEDED,
                ordinal=ordinal,
                now=finished_at,
                status_code=response.status_code,
                error_kind=None,
                retry_after=None,
                request_id=request_id,
                response_body=response.body,
            )
        if _retryable(response.status_code):
            return self._finish_failure(
                delivery,
                ordinal=ordinal,
                started_at=started_at,
                finished_at=finished_at,
                status_code=response.status_code,
                error_kind=f"http:{response.status_code}",
                retry_after=retry_after,
                request_id=request_id,
                response_body=response.body,
            )
        return self._commit(
            delivery,
            DispatchOutcome.DEAD,
            ordinal=ordinal,
            now=finished_at,
            status_code=response.status_code,
            error_kind=f"http:{response.status_code}",
            retry_after=retry_after,
            request_id=request_id,
            response_body=response.body,
        )

    def _finish_failure(
        self,
        delivery: ClaimedDelivery,
        *,
        ordinal: int,
        started_at: datetime,
        finished_at: datetime,
        status_code: int | None,
        error_kind: str,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
    ) -> DispatchResult:
        delay = self.config.retry.delay_seconds(
            delivery.delivery_id,
            ordinal,
            now=finished_at,
            retry_after=retry_after,
        )
        next_at = finished_at + timedelta(seconds=delay)
        allowed = self.config.retry.permits_retry(
            attempt_ordinal=ordinal,
            first_attempt_at=delivery.first_attempt_at or started_at,
            scheduled_at=next_at,
        )
        outcome = DispatchOutcome.RETRIED if allowed else DispatchOutcome.DEAD
        reason = error_kind if allowed else (
            "attempts_exhausted"
            if ordinal >= self.config.retry.max_attempts
            else "retry_window_exhausted"
        )
        return self._commit(
            delivery,
            outcome,
            ordinal=ordinal,
            now=finished_at,
            status_code=status_code,
            error_kind=reason,
            retry_after=retry_after,
            request_id=request_id,
            response_body=response_body,
            next_attempt_at=next_at if allowed else None,
        )

    def _commit(
        self,
        delivery: ClaimedDelivery,
        outcome: DispatchOutcome,
        *,
        ordinal: int,
        now: datetime,
        status_code: int | None,
        error_kind: str | None,
        retry_after: str | None,
        request_id: str | None,
        response_body: bytes,
        next_attempt_at: datetime | None = None,
    ) -> DispatchResult:
        try:
            self.repository.finish(
                delivery.delivery_id,
                delivery.lease_token,
                now=now,
                outcome=outcome,
                status_code=status_code,
                error_kind=error_kind,
                retry_after=retry_after,
                request_id=request_id,
                response_body=response_body,
                next_attempt_at=next_attempt_at,
            )
        except LeaseLost:
            return DispatchResult(
                delivery.delivery_id,
                DispatchOutcome.LEASE_LOST,
                attempt_ordinal=ordinal,
            )
        return DispatchResult(
            delivery.delivery_id,
            outcome,
            attempt_ordinal=ordinal,
            status_code=status_code,
            reason=error_kind,
        )

    def _audit(
        self,
        occurred_at: datetime,
        kind: str,
        delivery_id: str,
        details: Mapping[str, object],
    ) -> None:
        self.store.append_audit(
            occurred_at=occurred_at,
            kind=kind,
            delivery_id=delivery_id,
            details=dict(details),
        )


def _retryable(status_code: int) -> bool:
    return status_code in {408, 425, 429} or 500 <= status_code <= 599


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    for key, value in headers.items():
        if key.casefold() == folded:
            return value
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
