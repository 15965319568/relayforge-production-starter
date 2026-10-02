from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _support import BASE_TIME
from relayforge.control_db import ControlDatabase
from relayforge.management_api import ManagementApp
from relayforge.models import TransportResponse
from relayforge.platform import RelayForgePlatform
from relayforge.store import Store


class EnterpriseCase:
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "enterprise.sqlite3")
        self.store = Store(self.path)
        self.store.initialize()
        self.store.register_endpoint(
            tenant_id="org-a",
            endpoint_id="ep-a",
            url="https://a.example.test/hooks?token=fixture",
            secret="secret-a",
            max_in_flight=3,
            now=BASE_TIME,
        )
        self.store.register_endpoint(
            tenant_id="org-a",
            endpoint_id="ep-b",
            url="https://b.example.test/hooks",
            secret="secret-b",
            max_in_flight=2,
            now=BASE_TIME,
        )
        self.store.register_endpoint(
            tenant_id="org-b",
            endpoint_id="ep-c",
            url="https://c.example.test/hooks",
            secret="secret-c",
            max_in_flight=2,
            now=BASE_TIME,
        )
        self.database = ControlDatabase(self.path)
        self.database.initialize(now=BASE_TIME)
        self.platform = RelayForgePlatform(self.path, cursor_secret="cursor-secret")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_dead(
        self,
        *,
        endpoint_id: str = "ep-a",
        tenant_id: str = "org-a",
        suffix: str = "1",
        reason: str = "http:422",
        at=BASE_TIME,
    ) -> str:
        event_id = f"evt-dead-{suffix}"
        self.store.enqueue_event(
            tenant_id=tenant_id,
            topic="payment.failed",
            payload={"id": suffix},
            idempotency_key=f"dead-{suffix}",
            event_id=event_id,
            now=at,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            delivery_id = connection.execute(
                "SELECT id FROM deliveries WHERE event_id = ? AND endpoint_id = ?",
                (event_id, endpoint_id),
            ).fetchone()[0]
        claim = self.store.claim_selected(
            [delivery_id], worker_id="worker", now=at, lease_seconds=30
        )[0]
        self.store.start_attempt(delivery_id, claim.lease_token, now=at)
        self.store.finalize_dead(
            delivery_id,
            claim.lease_token,
            now=at + timedelta(seconds=1),
            reason=reason,
            outcome="terminal",
            status_code=422,
            retry_after=None,
            request_id=None,
            response_body=b"invalid",
        )
        return delivery_id

    def make_succeeded(self, *, suffix: str = "1", at=BASE_TIME) -> str:
        event_id = f"evt-success-{suffix}"
        self.store.enqueue_event(
            tenant_id="org-a",
            topic="payment.captured",
            payload={"id": suffix},
            idempotency_key=f"success-{suffix}",
            event_id=event_id,
            now=at,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            delivery_id = connection.execute(
                "SELECT id FROM deliveries WHERE event_id = ? ORDER BY endpoint_id LIMIT 1",
                (event_id,),
            ).fetchone()[0]
        claim = self.store.claim_selected(
            [delivery_id], worker_id="worker", now=at, lease_seconds=30
        )[0]
        self.store.start_attempt(delivery_id, claim.lease_token, now=at)
        self.store.finalize_success(
            delivery_id,
            claim.lease_token,
            now=at + timedelta(seconds=1),
            status_code=200,
            request_id="rq-success",
            response_body=b"sensitive response",
        )
        return delivery_id


def invoke_wsgi(
    app: ManagementApp,
    method: str,
    path: str,
    *,
    body: Any | None = None,
    query: str = "",
    headers: dict[str, str] | None = None,
    secret_environ: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
    raw = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else b""
    )
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
        "relayforge.environ": secret_environ or {},
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    captured: dict[str, Any] = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    chunks = app(environ, start_response)
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    return int(captured["status"].split()[0]), payload, captured["headers"]

