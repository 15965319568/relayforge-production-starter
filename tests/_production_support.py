from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _support import BASE_TIME, FakeClock, ScriptedTransport
from relayforge.auth_middleware import AuthMiddleware
from relayforge.delivery_processor import DeliveryProcessor
from relayforge.management_api import ManagementApp
from relayforge.platform import RelayForgePlatform
from relayforge.production_db import ProductionDatabase
from relayforge.store import Store


class ProductionCase:
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = str(self.root / "production.sqlite3")
        self.clock = FakeClock(BASE_TIME)
        self.store = Store(self.path)
        self.store.initialize()
        self.store.register_endpoint(
            tenant_id="org-a",
            endpoint_id="ep-a",
            url="https://a.example.test/hooks?token=fixture",
            secret="legacy-a",
            max_in_flight=3,
            now=BASE_TIME,
        )
        self.store.register_endpoint(
            tenant_id="org-b",
            endpoint_id="ep-b",
            url="https://b.example.test/hooks",
            secret="legacy-b",
            max_in_flight=2,
            now=BASE_TIME,
        )
        self.platform = RelayForgePlatform(self.path, cursor_secret="cursor-secret")
        self.platform.initialize(now=BASE_TIME)
        self.database = self.platform.database

    def tearDown(self) -> None:
        self.temp.cleanup()

    def enqueue(
        self,
        *,
        tenant: str = "org-a",
        endpoint: str = "ep-a",
        suffix: str = "1",
    ) -> str:
        event_id = f"evt-prod-{suffix}"
        self.store.enqueue_event(
            tenant_id=tenant,
            topic="invoice.paid",
            payload={"id": suffix, "amount": 42},
            idempotency_key=f"prod-{suffix}",
            event_id=event_id,
            now=self.clock(),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                """
                SELECT id FROM deliveries
                WHERE event_id = ? AND endpoint_id = ?
                """,
                (event_id, endpoint),
            ).fetchone()
        assert row is not None
        return str(row[0])

    def claim(self, delivery_id: str):
        return self.store.claim_selected(
            [delivery_id],
            worker_id="worker-a",
            now=self.clock(),
            lease_seconds=30,
            tenant_max_in_flight=10,
        )[0]

    def processor(self, transport) -> DeliveryProcessor:
        return DeliveryProcessor(
            repository=self.platform.dispatch,
            secret_ring=self.platform.secrets,
            store=self.store,
            transport=transport,
            clock=self.clock,
        )

    def authenticated_app(self):
        app = ManagementApp(self.platform, clock=self.clock)
        return AuthMiddleware(
            app,
            credentials=self.platform.credentials,
            database=self.database,
            clock=self.clock,
        )


def invoke(
    app,
    method: str,
    path: str,
    *,
    body: Any | None = None,
    query: str = "",
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
    raw = json.dumps(body).encode() if body is not None else b""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(raw),
        "relayforge.environ": {},
    }
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    captured: dict[str, Any] = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    payload = json.loads(b"".join(app(environ, start_response)))
    return int(captured["status"].split()[0]), payload, captured["headers"]
