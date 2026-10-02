from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable

from .control_db import ControlDatabase
from .control_models import AuditPage
from .cursors import CursorCodec
from .errors import ValidationError
from .redaction import sanitize_headers
from .timeutil import to_iso


class AuditExporter:
    def __init__(
        self,
        database: ControlDatabase,
        *,
        cursor_codec: CursorCodec,
        cursor_ttl_seconds: int = 900,
    ) -> None:
        if cursor_ttl_seconds <= 0:
            raise ValueError("cursor_ttl_seconds must be positive")
        self.database = database
        self.cursor_codec = cursor_codec
        self.cursor_ttl_seconds = cursor_ttl_seconds

    def page(
        self,
        *,
        now: datetime,
        tenant_id: str | None = None,
        kinds: Iterable[str] = (),
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> AuditPage:
        if not 1 <= limit <= 500:
            raise ValidationError("audit limit must be between 1 and 500")
        normalized_kinds = tuple(sorted({kind.strip() for kind in kinds if kind.strip()}))
        query = {
            "tenant_id": tenant_id,
            "kinds": normalized_kinds,
            "after": to_iso(after) if after else None,
            "before": to_iso(before) if before else None,
        }
        position_id = 0
        if cursor:
            position = self.cursor_codec.decode(
                cursor, scope="audit", query=query, now=now
            )
            if not isinstance(position.get("id"), int) or position["id"] < 0:
                raise ValidationError("audit cursor position is invalid")
            position_id = position["id"]

        clauses = ["audit.id > ?"]
        params: list[Any] = [position_id]
        if tenant_id is not None:
            clauses.append("event.tenant_id = ?")
            params.append(tenant_id)
        if normalized_kinds:
            placeholders = ",".join("?" for _ in normalized_kinds)
            clauses.append(f"audit.kind IN ({placeholders})")
            params.extend(normalized_kinds)
        if after is not None:
            clauses.append("audit.occurred_at >= ?")
            params.append(to_iso(after))
        if before is not None:
            clauses.append("audit.occurred_at < ?")
            params.append(to_iso(before))
        params.append(limit + 1)
        sql = f"""
            SELECT audit.id, audit.occurred_at, audit.kind, audit.delivery_id,
                   audit.details_json, event.tenant_id
            FROM audit_log audit
            LEFT JOIN deliveries delivery ON delivery.id = audit.delivery_id
            LEFT JOIN events event ON event.id = delivery.event_id
            WHERE {' AND '.join(clauses)}
            ORDER BY audit.id
            LIMIT ?
        """
        with self.database.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = tuple(_audit_item(row) for row in selected)
        next_cursor = None
        if has_more and selected:
            next_cursor = self.cursor_codec.encode(
                scope="audit",
                position={"id": selected[-1]["id"]},
                query=query,
                expires_at=now + timedelta(seconds=self.cursor_ttl_seconds),
            )
        return AuditPage(items, next_cursor)

    def ndjson(self, page: AuditPage) -> bytes:
        lines = [
            json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in page.items
        ]
        return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _audit_item(row) -> dict[str, Any]:
    details = json.loads(row["details_json"])
    safe_details = _sanitize_value(details)
    return {
        "id": row["id"],
        "occurred_at": row["occurred_at"],
        "kind": row["kind"],
        "delivery_id": row["delivery_id"],
        "tenant_id": row["tenant_id"],
        "details": safe_details,
    }


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            folded = key.casefold()
            if folded in {"secret", "lease_token", "token", "signature"}:
                sanitized[key] = "[REDACTED]"
            elif folded == "headers" and isinstance(item, dict):
                sanitized[key] = sanitize_headers(item)
            else:
                sanitized[key] = item
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value
