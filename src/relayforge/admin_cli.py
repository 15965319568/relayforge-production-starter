from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .control_db import ControlDatabase
from .diagnostics import DiagnosticService
from .errors import RelayForgeError, ValidationError
from .platform import RelayForgePlatform
from .timeutil import from_iso, utc_now
from .backup import BackupService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relayforge-admin")
    parser.add_argument("--db", required=True)
    parser.add_argument("--cursor-secret-env", default="RELAYFORGE_CURSOR_SECRET")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("diagnose")

    health = commands.add_parser("health")
    health.add_argument("mode", choices=("live", "ready"))

    credential = commands.add_parser("credential")
    credential_commands = credential.add_subparsers(dest="action", required=True)
    issue = credential_commands.add_parser("issue")
    issue.add_argument("--tenant")
    issue.add_argument("--scope", action="append", required=True)
    revoke = credential_commands.add_parser("revoke")
    revoke.add_argument("credential_id")

    consistency = commands.add_parser("consistency")
    consistency_commands = consistency.add_subparsers(dest="action", required=True)
    scan = consistency_commands.add_parser("scan")
    scan.add_argument("--stale-admission-seconds", type=int, default=120)

    repair = commands.add_parser("repair-plan")
    repair_commands = repair.add_subparsers(dest="action", required=True)
    repair_create = repair_commands.add_parser("create")
    repair_create.add_argument("source_run_id")
    repair_create.add_argument("--code", action="append")
    repair_apply = repair_commands.add_parser("apply")
    repair_apply.add_argument("plan_id")

    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="action", required=True)
    backup_create = backup_commands.add_parser("create")
    backup_create.add_argument("path")
    backup_restore = backup_commands.add_parser("restore")
    backup_restore.add_argument("source")
    backup_restore.add_argument("target")
    backup_restore.add_argument("--manifest")

    subscription = commands.add_parser("subscription")
    subscription_commands = subscription.add_subparsers(dest="action", required=True)
    add = subscription_commands.add_parser("add")
    add.add_argument("--tenant", required=True)
    add.add_argument("--endpoint", required=True)
    add.add_argument("--topic", required=True)
    add.add_argument("--filter", default="{}")
    listing = subscription_commands.add_parser("list")
    listing.add_argument("--tenant", required=True)

    secret = commands.add_parser("secret")
    secret_commands = secret.add_subparsers(dest="action", required=True)
    rotate = secret_commands.add_parser("rotate")
    rotate.add_argument("--endpoint", required=True)
    rotate.add_argument("--secret-env", required=True)
    rotate.add_argument("--overlap-seconds", type=int, default=300)

    rate = commands.add_parser("rate")
    rate_commands = rate.add_subparsers(dest="action", required=True)
    set_rate = rate_commands.add_parser("set")
    set_rate.add_argument("--tenant", required=True)
    set_rate.add_argument("--per-second", type=float, required=True)
    set_rate.add_argument("--burst", type=float, required=True)
    set_rate.add_argument("--max-in-flight", type=int, required=True)

    audit = commands.add_parser("audit")
    audit_commands = audit.add_subparsers(dest="action", required=True)
    export = audit_commands.add_parser("export")
    export.add_argument("--tenant")
    export.add_argument("--kind", action="append", default=[])
    export.add_argument("--after")
    export.add_argument("--before")
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--cursor")

    replay = commands.add_parser("replay-job")
    replay_commands = replay.add_subparsers(dest="action", required=True)
    create = replay_commands.add_parser("create")
    create.add_argument("--key", required=True)
    create.add_argument("--tenant")
    create.add_argument("--reason")
    create.add_argument("--before")
    run = replay_commands.add_parser("run")
    run.add_argument("job_id")
    run.add_argument("--limit", type=int, default=100)

    worker = commands.add_parser("worker")
    worker_commands = worker.add_subparsers(dest="action", required=True)
    register = worker_commands.add_parser("register")
    register.add_argument("--worker", required=True)
    register.add_argument("--metadata", default="{}")

    retention = commands.add_parser("retention")
    retention_commands = retention.add_subparsers(dest="action", required=True)
    run_retention = retention_commands.add_parser("run")
    run_retention.add_argument("--responses-before", required=True)
    run_retention.add_argument("--audit-before", required=True)
    run_retention.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = execute(args)
    except (RelayForgeError, ValueError, OSError, json.JSONDecodeError) as exc:
        _emit({"error": type(exc).__name__, "message": str(exc)[:300]}, sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - process boundary
        _emit({"error": "internal_error", "type": type(exc).__name__}, sys.stderr)
        return 1
    _emit(result, sys.stdout)
    return 0


def execute(args: argparse.Namespace) -> Any:
    secret = os.environ.get(args.cursor_secret_env)
    if not secret:
        raise ValidationError(
            f"environment variable {args.cursor_secret_env} is not set"
        )
    platform = RelayForgePlatform(args.db, cursor_secret=secret)
    now = utc_now()
    if args.command == "init":
        platform.initialize(now=now)
        return {"initialized": True, "database": str(Path(args.db))}
    platform.initialize(now=now)
    if args.command == "diagnose":
        return DiagnosticService(platform.database).snapshot(now=now)
    if args.command == "health":
        report = (
            platform.health.liveness()
            if args.mode == "live"
            else platform.health.readiness()
        )
        return {
            "status": report.status,
            "checked_at": report.checked_at.isoformat(),
            "checks": report.checks,
        }
    if args.command == "credential" and args.action == "issue":
        issued = platform.credentials.issue(
            tenant_id=args.tenant, scopes=args.scope, now=now
        )
        return {
            "id": issued.id,
            "tenant_id": issued.tenant_id,
            "scopes": sorted(issued.scopes),
            "token": issued.token,
        }
    if args.command == "credential" and args.action == "revoke":
        platform.credentials.revoke(args.credential_id, now=now)
        return {"id": args.credential_id, "revoked": True}
    if args.command == "consistency" and args.action == "scan":
        report = platform.consistency.scan(
            now=now, stale_admission_seconds=args.stale_admission_seconds
        )
        return {
            "run_id": report.run_id,
            "finding_count": len(report.findings),
            "findings": [
                {
                    "code": item.code,
                    "severity": item.severity.value,
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    "details": item.details,
                }
                for item in report.findings
            ],
        }
    if args.command == "repair-plan" and args.action == "create":
        plan = platform.repair_plans.create(
            args.source_run_id, now=now, codes=args.code
        )
        return {
            "id": plan.id,
            "state": plan.state.value,
            "action_count": plan.action_count,
        }
    if args.command == "repair-plan" and args.action == "apply":
        applied = platform.repairs.apply(args.plan_id, now=now)
        plan, _ = platform.repair_plans.get(args.plan_id)
        return {
            "id": plan.id,
            "state": plan.state.value,
            "applied_actions": applied,
        }
    if args.command == "backup" and args.action == "create":
        manifest = platform.backups.create(args.path, now=now)
        return {
            "id": manifest.id,
            "path": manifest.path,
            "sha256": manifest.sha256,
            "size_bytes": manifest.size_bytes,
        }
    if args.command == "backup" and args.action == "restore":
        restored = BackupService.restore(
            args.source, args.target, manifest_path=args.manifest
        )
        return {"restored": True, "path": str(restored)}
    if args.command == "subscription" and args.action == "add":
        expression = json.loads(args.filter)
        subscription = platform.subscriptions.create(
            tenant_id=args.tenant,
            endpoint_id=args.endpoint,
            topic_pattern=args.topic,
            filter_expression=expression,
            now=now,
        )
        return _subscription_json(subscription)
    if args.command == "subscription" and args.action == "list":
        return [
            _subscription_json(item)
            for item in platform.subscriptions.list_for_tenant(args.tenant)
        ]
    if args.command == "secret" and args.action == "rotate":
        next_secret = os.environ.get(args.secret_env)
        if not next_secret:
            raise ValidationError(f"environment variable {args.secret_env} is not set")
        version = platform.secrets.rotate(
            args.endpoint,
            new_secret=next_secret,
            overlap_seconds=args.overlap_seconds,
            now=now,
        )
        return {"endpoint_id": args.endpoint, "key_id": version.key_id}
    if args.command == "rate" and args.action == "set":
        policy = platform.rate_limits.configure(
            args.tenant,
            rate_per_second=args.per_second,
            burst=args.burst,
            max_in_flight=args.max_in_flight,
            now=now,
        )
        return {
            "tenant_id": policy.tenant_id,
            "rate_per_second": policy.rate_per_second,
            "burst": policy.burst,
            "max_in_flight": policy.max_in_flight,
        }
    if args.command == "audit" and args.action == "export":
        page = platform.audit.page(
            now=now,
            tenant_id=args.tenant,
            kinds=args.kind,
            after=from_iso(args.after) if args.after else None,
            before=from_iso(args.before) if args.before else None,
            limit=args.limit,
            cursor=args.cursor,
        )
        return {"items": list(page.items), "next_cursor": page.next_cursor}
    if args.command == "replay-job" and args.action == "create":
        job, created = platform.replays.create(
            idempotency_key=args.key,
            tenant_id=args.tenant,
            reason=args.reason,
            before=from_iso(args.before) if args.before else None,
            now=now,
        )
        return {**_job_json(job), "created": created}
    if args.command == "replay-job" and args.action == "run":
        result = platform.replays.run_batch(args.job_id, now=now, limit=args.limit)
        return {
            "job_id": result.job_id,
            "applied": result.applied,
            "skipped": result.skipped,
            "remaining": result.remaining,
            "state": result.state.value,
        }
    if args.command == "worker" and args.action == "register":
        metadata = json.loads(args.metadata)
        if not isinstance(metadata, dict):
            raise ValidationError("worker metadata must be an object")
        worker = platform.workers.register(args.worker, now=now, metadata=metadata)
        return {
            "worker_id": worker.worker_id,
            "generation": worker.generation,
            "draining": worker.draining,
        }
    if args.command == "retention" and args.action == "run":
        result = platform.retention.apply(
            response_before=from_iso(args.responses_before),
            audit_before=from_iso(args.audit_before),
            now=now,
            dry_run=args.dry_run,
        )
        return {
            "response_excerpts_cleared": result.response_excerpts_cleared,
            "audit_rows_deleted": result.audit_rows_deleted,
            "dry_run": result.dry_run,
        }
    raise ValidationError("unsupported admin command")


def _emit(value: Any, stream) -> None:
    print(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True),
        file=stream,
    )


def _subscription_json(subscription) -> dict[str, Any]:
    return {
        "id": subscription.id,
        "tenant_id": subscription.tenant_id,
        "endpoint_id": subscription.endpoint_id,
        "topic_pattern": subscription.topic_pattern,
        "filter": json.loads(subscription.filter_json),
        "enabled": subscription.enabled,
    }


def _job_json(job) -> dict[str, Any]:
    return {
        "id": job.id,
        "state": job.state.value,
        "total_count": job.total_count,
        "processed_count": job.processed_count,
        "skipped_count": job.skipped_count,
    }


if __name__ == "__main__":
    raise SystemExit(main())
