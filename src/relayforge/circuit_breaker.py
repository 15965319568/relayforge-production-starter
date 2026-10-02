from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from .control_db import ControlDatabase
from .control_models import CircuitDecision, CircuitState
from .errors import InvalidTransition, NotFound, ValidationError
from .timeutil import from_iso, to_iso


class PersistentCircuitBreaker:
    def __init__(
        self,
        database: ControlDatabase,
        *,
        failure_threshold: int = 5,
        recovery_seconds: int = 60,
    ) -> None:
        if failure_threshold <= 0 or recovery_seconds <= 0:
            raise ValueError("circuit thresholds must be positive")
        self.database = database
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds

    def allow(self, endpoint_id: str, *, now: datetime) -> CircuitDecision:
        now_iso = to_iso(now)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM endpoint_circuits WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"circuit for endpoint {endpoint_id} not found")
            state = CircuitState(row["state"])
            if state is CircuitState.CLOSED:
                return CircuitDecision(True, state, None, None)
            retry_at = from_iso(row["opened_until"]) if row["opened_until"] else None
            if state is CircuitState.OPEN and retry_at is not None and retry_at > now:
                return CircuitDecision(False, state, None, retry_at)
            if state is CircuitState.HALF_OPEN and row["probe_token"]:
                return CircuitDecision(False, state, None, retry_at)
            token = uuid.uuid4().hex
            connection.execute(
                """
                UPDATE endpoint_circuits
                SET state = 'half_open', probe_token = ?, updated_at = ?,
                    version = version + 1
                WHERE endpoint_id = ?
                """,
                (token, now_iso, endpoint_id),
            )
            return CircuitDecision(True, CircuitState.HALF_OPEN, token, retry_at)

    def record_success(
        self, endpoint_id: str, *, now: datetime, probe_token: str | None = None
    ) -> None:
        with self.database.write() as connection:
            row = self._row(connection, endpoint_id)
            self._validate_probe(row, probe_token)
            connection.execute(
                """
                UPDATE endpoint_circuits
                SET state = 'closed', failure_count = 0, opened_until = NULL,
                    probe_token = NULL, updated_at = ?, version = version + 1
                WHERE endpoint_id = ?
                """,
                (to_iso(now), endpoint_id),
            )

    def record_failure(
        self, endpoint_id: str, *, now: datetime, probe_token: str | None = None
    ) -> CircuitState:
        with self.database.write() as connection:
            row = self._row(connection, endpoint_id)
            self._validate_probe(row, probe_token)
            failures = int(row["failure_count"]) + 1
            should_open = (
                row["state"] == CircuitState.HALF_OPEN.value
                or failures >= self.failure_threshold
            )
            state = CircuitState.OPEN if should_open else CircuitState.CLOSED
            opened_until = (
                to_iso(now + timedelta(seconds=self.recovery_seconds))
                if should_open
                else None
            )
            connection.execute(
                """
                UPDATE endpoint_circuits
                SET state = ?, failure_count = ?, opened_until = ?,
                    probe_token = NULL, updated_at = ?, version = version + 1
                WHERE endpoint_id = ?
                """,
                (state.value, failures, opened_until, to_iso(now), endpoint_id),
            )
            return state

    @staticmethod
    def _row(connection, endpoint_id: str):
        row = connection.execute(
            "SELECT * FROM endpoint_circuits WHERE endpoint_id = ?",
            (endpoint_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"circuit for endpoint {endpoint_id} not found")
        return row

    @staticmethod
    def _validate_probe(row, probe_token: str | None) -> None:
        expected = row["probe_token"]
        if row["state"] == CircuitState.HALF_OPEN.value and probe_token is None:
            raise InvalidTransition("half-open circuit probe token is stale")
        if row["state"] != CircuitState.HALF_OPEN.value and probe_token is not None:
            raise InvalidTransition("probe token is only valid in half-open state")
