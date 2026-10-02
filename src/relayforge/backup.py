from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from .control_schema import CONTROL_SCHEMA_VERSION
from .errors import ValidationError
from .production_db import ProductionDatabase
from .production_models import BackupManifest
from .production_schema import PRODUCTION_SCHEMA_VERSION
from .schema import SCHEMA_VERSION
from .timeutil import to_iso


class BackupService:
    def __init__(self, database: ProductionDatabase) -> None:
        self.database = database

    def create(
        self,
        destination: str | Path,
        *,
        now: datetime,
        backup_id: str | None = None,
    ) -> BackupManifest:
        target = Path(destination).resolve()
        if target.exists():
            raise ValidationError(f"backup target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        identifier = backup_id or f"backup_{uuid.uuid4().hex}"
        temporary = _temporary_path(target)
        manifest_path = target.with_suffix(target.suffix + ".manifest.json")
        published = False
        try:
            with self.database.connection() as source:
                target_connection = sqlite3.connect(str(temporary), isolation_level=None)
                try:
                    source.backup(target_connection, pages=256, sleep=0.01)
                finally:
                    target_connection.close()
            _fsync_file(temporary)
            versions = validate_database(temporary)
            digest = _sha256(temporary)
            size = temporary.stat().st_size
            manifest = BackupManifest(
                identifier,
                str(target),
                digest,
                size,
                versions[0],
                versions[1],
                versions[2],
                now,
            )
            os.replace(temporary, target)
            published = True
            _write_manifest_atomic(manifest_path, manifest)
            with self.database.write() as connection:
                connection.execute(
                    """
                    INSERT INTO backup_records(
                        id, path, sha256, size_bytes, delivery_schema_version,
                        control_schema_version, production_schema_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.id,
                        manifest.path,
                        manifest.sha256,
                        manifest.size_bytes,
                        manifest.delivery_schema_version,
                        manifest.control_schema_version,
                        manifest.production_schema_version,
                        to_iso(now),
                    ),
                )
            return manifest
        except BaseException:
            _unlink(temporary)
            if published:
                _unlink(target)
                _unlink(manifest_path)
            raise

    @staticmethod
    def restore(
        backup: str | Path,
        target: str | Path,
        *,
        manifest_path: str | Path | None = None,
    ) -> Path:
        source = Path(backup).resolve()
        destination = Path(target).resolve()
        if not source.is_file():
            raise ValidationError(f"backup does not exist: {source}")
        if destination.exists():
            destination.unlink()
        sidecar = (
            Path(manifest_path).resolve()
            if manifest_path is not None
            else source.with_suffix(source.suffix + ".manifest.json")
        )
        manifest = _read_manifest(sidecar)
        if int(manifest["size_bytes"]) != source.stat().st_size:
            raise ValidationError("backup size does not match manifest")
        if str(manifest["sha256"]) != _sha256(source):
            raise ValidationError("backup checksum does not match manifest")
        versions = validate_database(source)
        expected = (
            int(manifest["delivery_schema_version"]),
            int(manifest["control_schema_version"]),
            int(manifest["production_schema_version"]),
        )
        if versions != expected:
            raise ValidationError("backup schema versions do not match manifest")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_path(destination)
        try:
            source_connection = sqlite3.connect(str(source), isolation_level=None)
            target_connection = sqlite3.connect(str(temporary), isolation_level=None)
            try:
                source_connection.backup(target_connection, pages=256, sleep=0.01)
            finally:
                target_connection.close()
                source_connection.close()
            _fsync_file(temporary)
            validate_database(temporary)
            os.replace(temporary, destination)
            return destination
        except BaseException:
            _unlink(temporary)
            raise


def validate_database(path: str | Path) -> tuple[int, int, int]:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValidationError(f"SQLite integrity check failed: {integrity[:3]}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ValidationError("SQLite foreign key check failed")
        versions = (
            _version(connection, "schema_meta"),
            _version(connection, "control_meta"),
            _version(connection, "production_meta"),
        )
        if versions != (
            SCHEMA_VERSION,
            CONTROL_SCHEMA_VERSION,
            PRODUCTION_SCHEMA_VERSION,
        ):
            raise ValidationError(f"unsupported schema versions: {versions}")
        return versions
    finally:
        connection.close()


def _version(connection: sqlite3.Connection, table: str) -> int:
    try:
        row = connection.execute(f"SELECT version FROM {table}").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValidationError(f"missing schema table {table}") from exc
    if row is None:
        raise ValidationError(f"schema table {table} is empty")
    return int(row[0])


def _temporary_path(target: Path) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    return Path(value)


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest_atomic(path: Path, manifest: BackupManifest) -> None:
    temporary = _temporary_path(path)
    try:
        payload = {
            "id": manifest.id,
            "path": manifest.path,
            "sha256": manifest.sha256,
            "size_bytes": manifest.size_bytes,
            "delivery_schema_version": manifest.delivery_schema_version,
            "control_schema_version": manifest.control_schema_version,
            "production_schema_version": manifest.production_schema_version,
            "created_at": to_iso(manifest.created_at),
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        _unlink(temporary)
        raise


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("backup manifest is missing or invalid") from exc
    required = {
        "sha256",
        "size_bytes",
        "delivery_schema_version",
        "control_schema_version",
        "production_schema_version",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValidationError("backup manifest is incomplete")
    return value


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
