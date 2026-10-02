from __future__ import annotations

import io
import json
import uuid
from datetime import datetime
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from .auth import CredentialService
from .errors import NotFound, ValidationError
from .production_db import ProductionDatabase
from .timeutil import utc_now


class AuthenticationFailure(Exception):
    pass


class AuthorizationFailure(Exception):
    pass


class AuthMiddleware:
    def __init__(
        self,
        app,
        *,
        credentials: CredentialService,
        database: ProductionDatabase,
        clock: Callable[[], datetime] = utc_now,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        self.app = app
        self.credentials = credentials
        self.database = database
        self.clock = clock
        self.max_body_bytes = max_body_bytes

    def __call__(self, environ: dict[str, Any], start_response):
        path = str(environ.get("PATH_INFO", "/"))
        request_id = str(environ.get("HTTP_X_REQUEST_ID", "")).strip()
        if not request_id or len(request_id) > 128:
            request_id = uuid.uuid4().hex
        environ["relayforge.request_id"] = request_id

        def contextual_start_response(status, headers):
            cleaned = [
                (name, value)
                for name, value in headers
                if name.casefold() != "x-request-id"
            ]
            cleaned.append(("X-Request-Id", request_id))
            return start_response(status, cleaned)

        if path in {"/live", "/ready"}:
            return self.app(environ, contextual_start_response)
        try:
            authorization = str(environ.get("HTTP_AUTHORIZATION", ""))
            if not authorization.startswith("Bearer "):
                raise AuthenticationFailure("bearer credential is required")
            try:
                principal = self.credentials.authenticate(
                    authorization.removeprefix("Bearer ").strip(), now=self.clock()
                )
            except ValidationError as exc:
                raise AuthenticationFailure("credential is invalid") from exc
            scope, tenant_id = self._request_policy(environ)
            try:
                self.credentials.authorize(
                    principal, scope=scope, tenant_id=tenant_id
                )
            except ValidationError as exc:
                raise AuthorizationFailure(str(exc)) from exc
            environ["relayforge.principal"] = principal
            return self.app(environ, contextual_start_response)
        except (AuthenticationFailure, AuthorizationFailure, ValidationError, NotFound) as exc:
            payload = json.dumps(
                {"error": type(exc).__name__, "message": str(exc)[:200]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if isinstance(exc, AuthenticationFailure):
                status = HTTPStatus.UNAUTHORIZED
            elif isinstance(exc, AuthorizationFailure):
                status = HTTPStatus.FORBIDDEN
            elif isinstance(exc, NotFound):
                status = HTTPStatus.NOT_FOUND
            else:
                status = HTTPStatus.BAD_REQUEST
            contextual_start_response(
                f"{status.value} {status.phrase}",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(payload))),
                    ("Cache-Control", "no-store"),
                ],
            )
            return [payload]

    def _request_policy(self, environ: dict[str, Any]) -> tuple[str, str | None]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        if method == "GET" and path == "/v1/audit":
            query = parse_qs(str(environ.get("QUERY_STRING", "")))
            tenant = _single(query.get("tenant_id"))
            return "audit:read", tenant
        if method == "POST" and path == "/v1/subscriptions":
            body = self._json_body(environ)
            return "subscriptions:write", _tenant(body)
        if method == "POST" and path == "/v1/replay-jobs":
            body = self._json_body(environ)
            return "replay:write", _optional_tenant(body)
        if method == "POST" and path.startswith("/v1/secrets/"):
            endpoint_id = path[len("/v1/secrets/") :].split("/", 1)[0]
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT tenant_id FROM endpoints WHERE id = ?", (endpoint_id,)
                ).fetchone()
            if row is None:
                return "secrets:rotate", None
            return "secrets:rotate", row["tenant_id"]
        if method == "POST" and path == "/v1/workers/register":
            return "workers:write", None
        if method == "POST" and path.startswith("/v1/replay-jobs/"):
            job_id = path[len("/v1/replay-jobs/") :].split("/", 1)[0]
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT tenant_id FROM replay_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            if row is None:
                return "replay:write", None
            return "replay:write", row["tenant_id"]
        return "operator", None

    def _json_body(self, environ: dict[str, Any]) -> Mapping[str, Any]:
        raw_length = str(environ.get("CONTENT_LENGTH", "0") or "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0 or length > self.max_body_bytes:
            raise ValidationError("request body is too large")
        stream = environ.get("wsgi.input")
        raw = stream.read(length) if stream is not None else b""
        environ["wsgi.input"] = io.BytesIO(raw)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body is invalid JSON") from exc
        if not isinstance(body, dict):
            raise ValidationError("request body must be a JSON object")
        return body


def _single(values: list[str] | None) -> str | None:
    if not values:
        return None
    if len(values) != 1:
        raise ValidationError("tenant_id must appear once")
    return values[0]


def _tenant(body: Mapping[str, Any]) -> str:
    tenant = body.get("tenant_id")
    if not isinstance(tenant, str) or not tenant:
        raise ValidationError("tenant_id is required")
    return tenant


def _optional_tenant(body: Mapping[str, Any]) -> str | None:
    value = body.get("tenant_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValidationError("tenant_id is invalid")
    return value
