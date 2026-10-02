from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

from .control_db import ControlDatabase
from .control_models import SecretVersion
from .errors import NotFound, ValidationError
from .timeutil import from_iso, to_iso


class SecretRing:
    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def rotate(
        self,
        endpoint_id: str,
        *,
        new_secret: str,
        now: datetime,
        overlap_seconds: int,
    ) -> SecretVersion:
        secret = new_secret.strip()
        if not secret:
            raise ValidationError("new secret is required")
        if overlap_seconds < 0:
            raise ValidationError("overlap_seconds cannot be negative")
        now_iso = to_iso(now)
        retire_at = to_iso(now + timedelta(seconds=overlap_seconds))
        with self.database.write() as connection:
            endpoint = connection.execute(
                "SELECT id FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            if endpoint is None:
                raise NotFound(f"endpoint {endpoint_id} not found")
            current = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS version
                FROM endpoint_secret_versions WHERE endpoint_id = ?
                """,
                (endpoint_id,),
            ).fetchone()["version"]
            version = int(current) + 1
            connection.execute(
                """
                UPDATE endpoint_secret_versions
                SET retired_at = ?
                WHERE endpoint_id = ? AND retired_at IS NULL
                """,
                (retire_at, endpoint_id),
            )
            key_id = f"v{version}"
            connection.execute(
                """
                INSERT INTO endpoint_secret_versions(
                    endpoint_id, version, key_id, secret, active_from, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (endpoint_id, version, key_id, secret, now_iso, now_iso),
            )
            connection.execute(
                "UPDATE endpoints SET secret = ? WHERE id = ?",
                (secret, endpoint_id),
            )
        return SecretVersion(
            endpoint_id=endpoint_id,
            version=version,
            key_id=key_id,
            secret=secret,
            active_from=now,
            retired_at=None,
            created_at=now,
        )

    def signing_secret(self, endpoint_id: str, *, now: datetime) -> SecretVersion:
        now_iso = to_iso(now)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM endpoint_secret_versions
                WHERE endpoint_id = ? AND active_from <= ?
                  AND retired_at IS NULL
                ORDER BY version DESC LIMIT 1
                """,
                (endpoint_id, now_iso),
            ).fetchone()
        if row is None:
            raise NotFound(f"active secret for endpoint {endpoint_id} not found")
        return _secret(row)

    def verification_secrets(
        self, endpoint_id: str, *, now: datetime
    ) -> list[SecretVersion]:
        now_iso = to_iso(now)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM endpoint_secret_versions
                WHERE endpoint_id = ? AND active_from <= ?
                  AND (retired_at IS NULL OR retired_at >= ?)
                ORDER BY version DESC
                """,
                (endpoint_id, now_iso, now_iso),
            ).fetchall()
        return [_secret(row) for row in rows]

    def sign(
        self,
        endpoint_id: str,
        *,
        now: datetime,
        timestamp: int,
        event_id: str,
        delivery_id: str,
        body: bytes,
    ) -> tuple[str, str]:
        key = self.signing_secret(endpoint_id, now=now)
        prefix = f"{key.key_id}.{timestamp}.{event_id}.".encode("utf-8")
        digest = hmac.new(key.secret.encode("utf-8"), prefix + body, hashlib.sha256)
        return key.key_id, "v2=" + digest.hexdigest()

    def verify(
        self,
        endpoint_id: str,
        *,
        now: datetime,
        key_id: str,
        signature: str,
        timestamp: int,
        event_id: str,
        delivery_id: str,
        body: bytes,
    ) -> bool:
        for key in self.verification_secrets(endpoint_id, now=now):
            if key.key_id != key_id:
                continue
            prefix = f"{key.key_id}.{timestamp}.{event_id}.".encode(
                "utf-8"
            )
            expected = "v2=" + hmac.new(
                key.secret.encode("utf-8"), prefix + body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
        return False


def _secret(row) -> SecretVersion:
    return SecretVersion(
        endpoint_id=row["endpoint_id"],
        version=row["version"],
        key_id=row["key_id"],
        secret=row["secret"],
        active_from=from_iso(row["active_from"]),
        retired_at=from_iso(row["retired_at"]) if row["retired_at"] else None,
        created_at=from_iso(row["created_at"]),
    )
