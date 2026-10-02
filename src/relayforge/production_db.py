from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .control_db import ControlDatabase
from .production_schema import PRODUCTION_SCHEMA_VERSION, apply_production_schema


class ProductionDatabase(ControlDatabase):
    def initialize(self, *, now: datetime | None = None) -> None:
        super().initialize(now=now)
        with self.write() as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'production_meta'
                """
            ).fetchone()
            if exists is None:
                apply_production_schema(connection)
                connection.execute(
                    "INSERT INTO production_meta(version) VALUES (?)",
                    (PRODUCTION_SCHEMA_VERSION,),
                )
                return
            row = connection.execute("SELECT version FROM production_meta").fetchone()
            if row is None:
                raise RuntimeError("production_meta is empty")
            if row["version"] != PRODUCTION_SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported production schema version {row['version']}"
                )
            apply_production_schema(connection)

