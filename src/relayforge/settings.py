from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class PlatformSettings:
    database_path: str
    cursor_secret_env: str
    busy_timeout_ms: int = 5_000
    audit_cursor_ttl_seconds: int = 900
    worker_stale_after_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.database_path:
            raise ValueError("database_path is required")
        if not self.cursor_secret_env:
            raise ValueError("cursor_secret_env is required")
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        if self.audit_cursor_ttl_seconds <= 0:
            raise ValueError("audit_cursor_ttl_seconds must be positive")
        if self.worker_stale_after_seconds <= 0:
            raise ValueError("worker_stale_after_seconds must be positive")

    def cursor_secret(self, environ: Mapping[str, str] | None = None) -> str:
        source = environ if environ is not None else os.environ
        value = source.get(self.cursor_secret_env)
        if not value:
            raise ValidationError(
                f"environment variable {self.cursor_secret_env} is not set"
            )
        return value


def load_settings(
    path: str | Path, *, environ: Mapping[str, str] | None = None
) -> PlatformSettings:
    source = environ if environ is not None else os.environ
    with Path(path).open("rb") as stream:
        document = tomllib.load(stream)
    service = document.get("service")
    if not isinstance(service, dict):
        raise ValidationError("settings require a [service] table")
    database_path = source.get(
        "RELAYFORGE_DATABASE", str(service.get("database_path", ""))
    )
    cursor_secret_env = str(service.get("cursor_secret_env", ""))
    return PlatformSettings(
        database_path=database_path,
        cursor_secret_env=cursor_secret_env,
        busy_timeout_ms=_int_setting(service, "busy_timeout_ms", 5_000),
        audit_cursor_ttl_seconds=_int_setting(
            service, "audit_cursor_ttl_seconds", 900
        ),
        worker_stale_after_seconds=_int_setting(
            service, "worker_stale_after_seconds", 120
        ),
    )


def _int_setting(source: Mapping[str, object], key: str, default: int) -> int:
    value = source.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{key} must be an integer")
    return value

