from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Iterable

from .errors import NotFound, ValidationError
from .production_db import ProductionDatabase
from .production_models import IssuedCredential, Principal
from .timeutil import to_iso


ALLOWED_SCOPES = frozenset(
    {
        "events:write",
        "subscriptions:write",
        "secrets:rotate",
        "audit:read",
        "replay:write",
        "workers:write",
        "maintenance:write",
        "operator",
    }
)

PBKDF2_ITERATIONS = 180_000


class CredentialService:
    def __init__(self, database: ProductionDatabase) -> None:
        self.database = database

    def issue(
        self,
        *,
        tenant_id: str | None,
        scopes: Iterable[str],
        now: datetime,
        credential_id: str | None = None,
    ) -> IssuedCredential:
        normalized = frozenset(scope.strip() for scope in scopes if scope.strip())
        if not normalized:
            raise ValidationError("at least one credential scope is required")
        unknown = normalized - ALLOWED_SCOPES
        if unknown:
            raise ValidationError(f"unsupported credential scopes: {sorted(unknown)}")
        if "operator" in normalized and tenant_id is not None:
            raise ValidationError("operator credential cannot be tenant scoped")
        if "operator" not in normalized and not tenant_id:
            raise ValidationError("non-operator credential requires tenant_id")
        identifier = credential_id or "cred_" + secrets.token_hex(12)
        raw_secret = secrets.token_urlsafe(32)
        token = f"rfg_{identifier}.{raw_secret}"
        salt = secrets.token_bytes(16)
        digest = _derive(raw_secret, salt)
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO api_credentials(
                    id, tenant_id, token_hash, salt, scopes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    tenant_id,
                    digest,
                    salt,
                    json.dumps(sorted(normalized), separators=(",", ":")),
                    to_iso(now),
                ),
            )
        return IssuedCredential(identifier, tenant_id, normalized, token, now)

    def authenticate(self, token: str, *, now: datetime) -> Principal:
        identifier, raw_secret = _split_token(token)
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM api_credentials WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise ValidationError("credential is invalid")
            expected = bytes(row["token_hash"])
            supplied = _derive(raw_secret, bytes(row["salt"]))
            if not hmac.compare_digest(expected, supplied):
                raise ValidationError("credential is invalid")
            connection.execute(
                "UPDATE api_credentials SET last_used_at = ? WHERE id = ?",
                (to_iso(now), identifier),
            )
            scopes = frozenset(json.loads(row["scopes_json"]))
            return Principal(identifier, row["tenant_id"], scopes)

    def revoke(self, credential_id: str, *, now: datetime) -> None:
        with self.database.write() as connection:
            updated = connection.execute(
                """
                UPDATE api_credentials SET revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (to_iso(now), credential_id),
            )
            if updated.rowcount != 1:
                raise NotFound(f"active credential {credential_id} not found")

    @staticmethod
    def authorize(
        principal: Principal, *, scope: str, tenant_id: str | None = None
    ) -> None:
        if scope not in principal.scopes and "operator" not in principal.scopes:
            raise ValidationError("credential scope is insufficient")
        if not principal.is_operator and tenant_id is not None:
            if principal.tenant_id != tenant_id:
                raise ValidationError("credential tenant does not match request")


def _derive(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )


def _split_token(token: str) -> tuple[str, str]:
    if not token.startswith("rfg_") or "." not in token:
        raise ValidationError("credential is invalid")
    prefix, raw_secret = token.split(".", 1)
    identifier = prefix.removeprefix("rfg_")
    if (
        not identifier
        or not raw_secret
        or len(identifier) > 96
        or len(raw_secret) > 256
        or not all(ch.isalnum() or ch in "_-" for ch in identifier)
    ):
        raise ValidationError("credential is invalid")
    return identifier, raw_secret
