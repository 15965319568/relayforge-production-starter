from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from .production_db import ProductionDatabase
from .production_models import (
    ConsistencyFinding,
    ConsistencyReport,
    FindingSeverity,
)
from .timeutil import from_iso, to_iso


class ConsistencyScanner:
    """Read-only invariant scanner; scan persistence happens after collection."""

    def __init__(self, database: ProductionDatabase) -> None:
        self.database = database

    def scan(
        self,
        *,
        now: datetime,
        stale_admission_seconds: int = 120,
        run_id: str | None = None,
    ) -> ConsistencyReport:
        if stale_admission_seconds <= 0:
            raise ValueError("stale_admission_seconds must be positive")
        started_at = now
        with self.database.connection() as connection:
            findings = self.collect(
                connection,
                now=now,
                stale_admission_seconds=stale_admission_seconds,
            )
        completed_at = now
        identifier = run_id or f"scan_{uuid.uuid4().hex}"
        fingerprint = findings_fingerprint(findings)
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO consistency_runs(
                    id, started_at, completed_at, fingerprint, finding_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    to_iso(started_at),
                    to_iso(completed_at),
                    fingerprint,
                    len(findings),
                ),
            )
            for ordinal, finding in enumerate(findings, 1):
                connection.execute(
                    """
                    INSERT INTO consistency_findings(
                        run_id, ordinal, code, severity, entity_type,
                        entity_id, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        ordinal,
                        finding.code,
                        finding.severity.value,
                        finding.entity_type,
                        finding.entity_id,
                        _canonical(finding.details),
                    ),
                )
        return ConsistencyReport(
            identifier, started_at, completed_at, tuple(findings)
        )

    def fingerprint(
        self,
        connection,
        *,
        now: datetime,
        stale_admission_seconds: int = 120,
    ) -> str:
        return findings_fingerprint(
            self.collect(
                connection,
                now=now,
                stale_admission_seconds=stale_admission_seconds,
            )
        )

    def collect(
        self,
        connection,
        *,
        now: datetime,
        stale_admission_seconds: int,
    ) -> list[ConsistencyFinding]:
        findings: list[ConsistencyFinding] = []
        self._delivery_findings(connection, findings)
        self._dead_letter_findings(connection, findings)
        self._replay_findings(connection, findings)
        self._control_findings(connection, findings)
        self._admission_findings(
            connection,
            findings,
            now=now,
            stale_after=stale_admission_seconds,
        )
        return findings

    @staticmethod
    def _delivery_findings(connection, findings: list[ConsistencyFinding]) -> None:
        for row in connection.execute(
            """
            SELECT a.id, a.delivery_id FROM attempts a
            LEFT JOIN deliveries d ON d.id = a.delivery_id
            WHERE d.id IS NULL ORDER BY a.id
            """
        ):
            findings.append(
                _finding(
                    "ORPHAN_ATTEMPT",
                    FindingSeverity.CRITICAL,
                    "attempt",
                    str(row["id"]),
                    {"delivery_id": row["delivery_id"]},
                )
            )
        for row in connection.execute(
            """
            SELECT id, state, version, lease_owner, lease_token, lease_expires_at
            FROM deliveries
            WHERE state IN ('succeeded', 'dead')
              AND (lease_owner IS NOT NULL OR lease_token IS NOT NULL
                   OR lease_expires_at IS NOT NULL)
            ORDER BY id
            """
        ):
            findings.append(
                _finding(
                    "TERMINAL_ACTIVE_LEASE",
                    FindingSeverity.CRITICAL,
                    "delivery",
                    row["id"],
                    {"state": row["state"], "expected_version": row["version"]},
                )
            )
        for row in connection.execute(
            """
            SELECT d.id, d.attempt_count, d.version,
                   COALESCE(MAX(a.ordinal), 0) AS highest_ordinal
            FROM deliveries d LEFT JOIN attempts a ON a.delivery_id = d.id
            GROUP BY d.id
            HAVING d.attempt_count < COALESCE(MAX(a.ordinal), 0)
            ORDER BY d.id
            """
        ):
            findings.append(
                _finding(
                    "ATTEMPT_COUNT_BEHIND",
                    FindingSeverity.WARNING,
                    "delivery",
                    row["id"],
                    {
                        "attempt_count": row["attempt_count"],
                        "highest_ordinal": row["highest_ordinal"],
                        "expected_version": row["version"],
                    },
                )
            )

    @staticmethod
    def _dead_letter_findings(connection, findings: list[ConsistencyFinding]) -> None:
        for row in connection.execute(
            """
            SELECT d.id, d.last_error, d.completed_at, d.version
            FROM deliveries d LEFT JOIN dead_letters dl ON dl.delivery_id = d.id
            WHERE d.state = 'dead' AND dl.delivery_id IS NULL ORDER BY d.id
            """
        ):
            findings.append(
                _finding(
                    "DEAD_MISSING_LETTER",
                    FindingSeverity.CRITICAL,
                    "delivery",
                    row["id"],
                    {
                        "reason": row["last_error"] or "unknown",
                        "dead_at": row["completed_at"],
                        "expected_version": row["version"],
                    },
                )
            )
        for row in connection.execute(
            """
            SELECT d.id, d.state, d.version FROM dead_letters dl
            JOIN deliveries d ON d.id = dl.delivery_id
            WHERE d.state != 'dead' ORDER BY d.id
            """
        ):
            findings.append(
                _finding(
                    "NON_DEAD_HAS_LETTER",
                    FindingSeverity.WARNING,
                    "delivery",
                    row["id"],
                    {"state": row["state"], "expected_version": row["version"]},
                )
            )

    @staticmethod
    def _replay_findings(connection, findings: list[ConsistencyFinding]) -> None:
        for row in connection.execute(
            """
            SELECT j.id, j.total_count, j.processed_count, j.skipped_count,
                   COUNT(i.delivery_id) AS actual_total,
                   SUM(CASE WHEN i.state = 'applied' THEN 1 ELSE 0 END) AS actual_applied,
                   SUM(CASE WHEN i.state = 'skipped' THEN 1 ELSE 0 END) AS actual_skipped
            FROM replay_jobs j LEFT JOIN replay_job_items i ON i.job_id = j.id
            GROUP BY j.id
            HAVING j.total_count != COUNT(i.delivery_id)
                OR j.processed_count != SUM(CASE WHEN i.state = 'applied' THEN 1 ELSE 0 END)
                OR j.skipped_count != SUM(CASE WHEN i.state = 'skipped' THEN 1 ELSE 0 END)
            ORDER BY j.id
            """
        ):
            findings.append(
                _finding(
                    "REPLAY_COUNTER_MISMATCH",
                    FindingSeverity.WARNING,
                    "replay_job",
                    row["id"],
                    {
                        "stored": [
                            row["total_count"],
                            row["processed_count"],
                            row["skipped_count"],
                        ],
                        "actual": [
                            row["actual_total"],
                            row["actual_applied"] or 0,
                            row["actual_skipped"] or 0,
                        ],
                    },
                )
            )

    @staticmethod
    def _control_findings(connection, findings: list[ConsistencyFinding]) -> None:
        for row in connection.execute(
            """
            SELECT s.id, s.tenant_id AS subscription_tenant,
                   e.tenant_id AS endpoint_tenant
            FROM subscriptions s JOIN endpoints e ON e.id = s.endpoint_id
            WHERE s.tenant_id != e.tenant_id ORDER BY s.id
            """
        ):
            findings.append(
                _finding(
                    "SUBSCRIPTION_TENANT_MISMATCH",
                    FindingSeverity.CRITICAL,
                    "subscription",
                    row["id"],
                    {
                        "subscription_tenant": row["subscription_tenant"],
                        "endpoint_tenant": row["endpoint_tenant"],
                    },
                )
            )
        for row in connection.execute(
            """
            SELECT p.tenant_id, b.tokens, p.burst, b.version
            FROM tenant_bucket_state b
            JOIN tenant_rate_policies p ON p.tenant_id = b.tenant_id
            WHERE b.tokens < 0 OR b.tokens > p.burst ORDER BY p.tenant_id
            """
        ):
            findings.append(
                _finding(
                    "BUCKET_OUT_OF_RANGE",
                    FindingSeverity.WARNING,
                    "tenant_bucket",
                    row["tenant_id"],
                    {
                        "tokens": row["tokens"],
                        "burst": row["burst"],
                        "expected_version": row["version"],
                    },
                )
            )
        for row in connection.execute(
            """
            SELECT endpoint_id, state, probe_token, version
            FROM endpoint_circuits
            WHERE (state = 'half_open' AND probe_token IS NULL)
               OR (state != 'half_open' AND probe_token IS NOT NULL)
            ORDER BY endpoint_id
            """
        ):
            findings.append(
                _finding(
                    "CIRCUIT_PROBE_MISMATCH",
                    FindingSeverity.CRITICAL,
                    "endpoint_circuit",
                    row["endpoint_id"],
                    {
                        "state": row["state"],
                        "has_probe": row["probe_token"] is not None,
                        "expected_version": row["version"],
                    },
                )
            )
        for row in connection.execute(
            """
            SELECT endpoint_id, COUNT(*) AS current_count
            FROM endpoint_secret_versions
            WHERE retired_at IS NULL GROUP BY endpoint_id
            HAVING COUNT(*) != 1 ORDER BY endpoint_id
            """
        ):
            findings.append(
                _finding(
                    "SECRET_CURRENT_COUNT",
                    FindingSeverity.CRITICAL,
                    "endpoint",
                    row["endpoint_id"],
                    {"current_count": row["current_count"]},
                )
            )

    @staticmethod
    def _admission_findings(
        connection,
        findings: list[ConsistencyFinding],
        *,
        now: datetime,
        stale_after: int,
    ) -> None:
        cutoff = to_iso(now - timedelta(seconds=stale_after))
        now_iso = to_iso(now)
        for row in connection.execute(
            """
            SELECT a.delivery_id, a.updated_at, d.state, d.version
            FROM dispatch_admissions a
            LEFT JOIN deliveries d ON d.id = a.delivery_id
            WHERE a.state = 'admitted' AND a.updated_at <= ?
              AND (d.id IS NULL OR d.state != 'in_flight'
                   OR d.lease_expires_at IS NULL OR d.lease_expires_at <= ?)
            ORDER BY a.delivery_id
            """,
            (cutoff, now_iso),
        ):
            findings.append(
                _finding(
                    "STALE_DISPATCH_ADMISSION",
                    FindingSeverity.WARNING,
                    "dispatch_admission",
                    row["delivery_id"],
                    {
                        "updated_at": row["updated_at"],
                        "delivery_state": row["state"],
                        "expected_version": row["version"],
                    },
                )
            )


def findings_fingerprint(findings: list[ConsistencyFinding]) -> str:
    encoded = _canonical(
        [
            {
                "code": finding.code,
                "severity": finding.severity.value,
                "entity_type": finding.entity_type,
                "entity_id": finding.entity_id,
                "details": finding.details,
            }
            for finding in findings
        ]
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finding(
    code: str,
    severity: FindingSeverity,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any],
) -> ConsistencyFinding:
    return ConsistencyFinding(code, severity, entity_type, entity_id, details)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
