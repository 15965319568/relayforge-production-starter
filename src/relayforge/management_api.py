from __future__ import annotations

import json
from datetime import datetime
from http import HTTPStatus
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs

from .errors import (
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
    RelayForgeError,
    ValidationError,
)
from .platform import RelayForgePlatform
from .timeutil import from_iso, utc_now


StartResponse = Callable[[str, list[tuple[str, str]]], None]


class ManagementApp:
    def __init__(
        self,
        platform: RelayForgePlatform,
        *,
        clock: Callable[[], datetime] = utc_now,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        self.platform = platform
        self.clock = clock
        self.max_body_bytes = max_body_bytes

    def __call__(self, environ: Mapping[str, Any], start_response: StartResponse):
        try:
            status, payload = self.dispatch(environ)
        except NotFound as exc:
            status, payload = HTTPStatus.NOT_FOUND, _error(exc)
        except (IdempotencyConflict, InvalidTransition) as exc:
            status, payload = HTTPStatus.CONFLICT, _error(exc)
        except (RelayForgeError, ValueError, json.JSONDecodeError) as exc:
            status, payload = HTTPStatus.BAD_REQUEST, _error(exc)
        except Exception as exc:  # pragma: no cover - defensive WSGI boundary
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            payload = {"error": "internal_error", "type": type(exc).__name__}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        start_response(
            f"{status.value} {status.phrase}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [encoded]

    def dispatch(self, environ: Mapping[str, Any]) -> tuple[HTTPStatus, Any]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        now = self.clock()
        if method == "GET" and path == "/live":
            report = self.platform.health.liveness()
            return (
                HTTPStatus.OK if report.status == "ok" else HTTPStatus.SERVICE_UNAVAILABLE,
                _health_json(report),
            )
        if method == "GET" and path == "/ready":
            report = self.platform.health.readiness()
            return (
                HTTPStatus.OK if report.status == "ready" else HTTPStatus.SERVICE_UNAVAILABLE,
                _health_json(report),
            )
        if method == "GET" and path == "/health":
            report = self.platform.health.liveness()
            return HTTPStatus.OK, _health_json(report)
        if method == "POST" and path == "/v1/credentials":
            body = self._body(environ)
            scopes = body.get("scopes")
            if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
                raise ValidationError("scopes must be an array of strings")
            credential = self.platform.credentials.issue(
                tenant_id=_optional_string(body.get("tenant_id")),
                scopes=scopes,
                now=now,
            )
            return HTTPStatus.CREATED, {
                "id": credential.id,
                "tenant_id": credential.tenant_id,
                "scopes": sorted(credential.scopes),
                "token": credential.token,
            }
        if method == "POST" and path.startswith("/v1/credentials/") and path.endswith("/revoke"):
            credential_id = path[len("/v1/credentials/") : -len("/revoke")].strip("/")
            self.platform.credentials.revoke(credential_id, now=now)
            return HTTPStatus.OK, {"id": credential_id, "revoked": True}
        if method == "POST" and path == "/v1/subscriptions":
            body = self._body(environ)
            subscription = self.platform.subscriptions.create(
                tenant_id=_string(body, "tenant_id"),
                endpoint_id=_string(body, "endpoint_id"),
                topic_pattern=_string(body, "topic_pattern"),
                filter_expression=body.get("filter"),
                now=now,
            )
            return HTTPStatus.CREATED, {
                "id": subscription.id,
                "tenant_id": subscription.tenant_id,
                "endpoint_id": subscription.endpoint_id,
                "topic_pattern": subscription.topic_pattern,
                "enabled": subscription.enabled,
            }
        if method == "POST" and path.startswith("/v1/secrets/") and path.endswith(
            "/rotate"
        ):
            endpoint_id = path[len("/v1/secrets/") : -len("/rotate")].strip("/")
            body = self._body(environ)
            secret_env = _string(body, "secret_env")
            secret = str(environ.get("relayforge.environ", {}).get(secret_env, ""))
            if not secret:
                raise ValidationError(f"secret environment {secret_env} is not set")
            version = self.platform.secrets.rotate(
                endpoint_id,
                new_secret=secret,
                now=now,
                overlap_seconds=_integer(body, "overlap_seconds", 300),
            )
            return HTTPStatus.CREATED, {
                "endpoint_id": endpoint_id,
                "key_id": version.key_id,
                "active_from": version.active_from.isoformat(),
            }
        if method == "GET" and path == "/v1/audit":
            query = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True)
            kinds = tuple(query.get("kind", []))
            page = self.platform.audit.page(
                now=now,
                tenant_id=_query_one(query, "tenant_id"),
                kinds=kinds,
                after=_query_time(query, "after"),
                before=_query_time(query, "before"),
                limit=int(_query_one(query, "limit") or 100),
                cursor=_query_one(query, "cursor"),
            )
            return HTTPStatus.OK, {
                "items": list(page.items),
                "next_cursor": page.next_cursor,
            }
        if method == "POST" and path == "/v1/replay-jobs":
            body = self._body(environ)
            job, created = self.platform.replays.create(
                idempotency_key=_header(environ, "HTTP_IDEMPOTENCY_KEY"),
                tenant_id=_optional_string(body.get("tenant_id")),
                reason=_optional_string(body.get("reason")),
                before=(
                    from_iso(_string(body, "before")) if body.get("before") else None
                ),
                now=now,
            )
            return (HTTPStatus.CREATED if created else HTTPStatus.OK), _job_json(job)
        if method == "POST" and path.startswith("/v1/replay-jobs/") and path.endswith(
            "/run"
        ):
            job_id = path[len("/v1/replay-jobs/") : -len("/run")].strip("/")
            body = self._body(environ)
            result = self.platform.replays.run_batch(
                job_id, now=now, limit=_integer(body, "limit", 100)
            )
            return HTTPStatus.OK, {
                "job_id": result.job_id,
                "applied": result.applied,
                "skipped": result.skipped,
                "remaining": result.remaining,
                "state": result.state.value,
            }
        if method == "POST" and path == "/v1/workers/register":
            body = self._body(environ)
            worker = self.platform.workers.register(
                _string(body, "worker_id"),
                now=now,
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            )
            return HTTPStatus.CREATED, {
                "worker_id": worker.worker_id,
                "generation": worker.generation,
                "draining": worker.draining,
            }
        if method == "POST" and path == "/v1/maintenance/consistency-scans":
            body = self._body(environ)
            report = self.platform.consistency.scan(
                now=now,
                stale_admission_seconds=_integer(body, "stale_admission_seconds", 120),
            )
            return HTTPStatus.CREATED, {
                "run_id": report.run_id,
                "finding_count": len(report.findings),
                "findings": [
                    {
                        "code": item.code,
                        "severity": item.severity.value,
                        "entity_type": item.entity_type,
                        "entity_id": item.entity_id,
                        "details": item.details,
                    }
                    for item in report.findings
                ],
            }
        if method == "POST" and path == "/v1/maintenance/repair-plans":
            body = self._body(environ)
            codes = body.get("codes")
            if codes is not None and (
                not isinstance(codes, list) or not all(isinstance(x, str) for x in codes)
            ):
                raise ValidationError("codes must be an array of strings")
            plan = self.platform.repair_plans.create(
                _string(body, "source_run_id"), now=now, codes=codes
            )
            return HTTPStatus.CREATED, {
                "id": plan.id,
                "state": plan.state.value,
                "action_count": plan.action_count,
            }
        if method == "POST" and path.startswith("/v1/maintenance/repair-plans/") and path.endswith("/apply"):
            plan_id = path[len("/v1/maintenance/repair-plans/") : -len("/apply")].strip("/")
            applied = self.platform.repairs.apply(plan_id, now=now)
            plan, _ = self.platform.repair_plans.get(plan_id)
            return HTTPStatus.OK, {
                "id": plan.id,
                "state": plan.state.value,
                "applied_actions": applied,
            }
        if method == "POST" and path == "/v1/maintenance/backups":
            body = self._body(environ)
            manifest = self.platform.backups.create(_string(body, "path"), now=now)
            return HTTPStatus.CREATED, {
                "id": manifest.id,
                "path": manifest.path,
                "sha256": manifest.sha256,
                "size_bytes": manifest.size_bytes,
            }
        raise NotFound(f"route {method} {path} not found")

    def _body(self, environ: Mapping[str, Any]) -> dict[str, Any]:
        raw_length = str(environ.get("CONTENT_LENGTH", "0") or "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0 or length > self.max_body_bytes:
            raise ValidationError("request body is too large")
        stream = environ.get("wsgi.input")
        raw = stream.read(length) if stream is not None else b""
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValidationError("request body must be a JSON object")
        return parsed


def _error(exc: BaseException) -> dict[str, str]:
    return {"error": type(exc).__name__, "message": str(exc)[:300]}


def _string(body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} is required")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("optional string cannot be blank")
    return value.strip()


def _integer(body: Mapping[str, Any], key: str, default: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{key} must be an integer")
    return value


def _header(environ: Mapping[str, Any], key: str) -> str:
    value = str(environ.get(key, "")).strip()
    if not value:
        raise ValidationError(f"{key.removeprefix('HTTP_')} header is required")
    return value


def _query_one(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1 or not values[0]:
        raise ValidationError(f"query parameter {key} must appear once")
    return values[0]


def _query_time(query: Mapping[str, list[str]], key: str) -> datetime | None:
    value = _query_one(query, key)
    return from_iso(value) if value is not None else None


def _job_json(job) -> dict[str, Any]:
    return {
        "id": job.id,
        "state": job.state.value,
        "tenant_id": job.tenant_id,
        "reason": job.reason,
        "before": job.before_at.isoformat() if job.before_at else None,
        "total_count": job.total_count,
        "processed_count": job.processed_count,
        "skipped_count": job.skipped_count,
    }


def _health_json(report) -> dict[str, Any]:
    return {
        "status": report.status,
        "checked_at": report.checked_at.isoformat(),
        "checks": report.checks,
    }
