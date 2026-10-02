from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from relayforge.models import OutboundRequest, TransportResponse
from relayforge.store import Store


BASE_TIME = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


class StoreFixture:
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "relayforge.sqlite3")
        self.store = Store(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class FakeClock:
    def __init__(self, current: datetime = BASE_TIME) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


class ScriptedTransport:
    def __init__(self, *outcomes: TransportResponse | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[OutboundRequest] = []

    def send(self, request: OutboundRequest) -> TransportResponse:
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("unexpected transport call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def seed_delivery(
    store: Store,
    *,
    tenant: str = "tenant-a",
    endpoint_id: str = "ep-a",
    event_id: str = "evt-a",
    key: str = "key-a",
    now: datetime = BASE_TIME,
    max_in_flight: int = 4,
    url: str = "https://example.test/hooks?secret=query-secret",
    secret: str = "signing-secret",
    payload: Any | None = None,
) -> str:
    store.register_endpoint(
        tenant_id=tenant,
        url=url,
        secret=secret,
        max_in_flight=max_in_flight,
        endpoint_id=endpoint_id,
        now=now,
    )
    store.enqueue_event(
        tenant_id=tenant,
        topic="invoice.paid",
        payload=payload if payload is not None else {"invoice": "inv-1", "amount": 42},
        idempotency_key=key,
        event_id=event_id,
        now=now,
    )
    return first_delivery_id(store.path, event_id=event_id, endpoint_id=endpoint_id)


def first_delivery_id(path: str, *, event_id: str, endpoint_id: str) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT id FROM deliveries WHERE event_id = ? AND endpoint_id = ?",
            (event_id, endpoint_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def query_one(path: str, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(query, params).fetchone()
    assert row is not None
    return row
