from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import ServiceConfig
from .engine import DeliveryEngine
from .errors import RelayForgeError, ValidationError
from .store import Store
from .timeutil import from_iso, utc_now
from .transport import UrllibTransport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relayforge")
    parser.add_argument("--db", required=True, help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    endpoint = commands.add_parser("endpoint")
    endpoint_commands = endpoint.add_subparsers(dest="endpoint_command", required=True)
    endpoint_add = endpoint_commands.add_parser("add")
    endpoint_add.add_argument("--tenant", required=True)
    endpoint_add.add_argument("--url", required=True)
    endpoint_add.add_argument("--secret-env", required=True)
    endpoint_add.add_argument("--max-in-flight", type=int, default=4)

    event = commands.add_parser("event")
    event_commands = event.add_subparsers(dest="event_command", required=True)
    enqueue = event_commands.add_parser("enqueue")
    enqueue.add_argument("--tenant", required=True)
    enqueue.add_argument("--topic", required=True)
    enqueue.add_argument("--key", required=True)
    enqueue.add_argument("--payload", required=True)

    worker = commands.add_parser("worker")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    run_once = worker_commands.add_parser("run-once")
    run_once.add_argument("--worker", required=True)
    run_once.add_argument("--limit", type=int, default=25)

    delivery = commands.add_parser("delivery")
    delivery_commands = delivery.add_subparsers(dest="delivery_command", required=True)
    inspect = delivery_commands.add_parser("inspect")
    inspect.add_argument("delivery_id")

    dead = commands.add_parser("dead-letter")
    dead_commands = dead.add_subparsers(dest="dead_command", required=True)
    dead_list = dead_commands.add_parser("list")
    dead_list.add_argument("--tenant")
    dead_list.add_argument("--limit", type=int, default=100)
    replay = dead_commands.add_parser("replay")
    replay.add_argument("--tenant")
    replay.add_argument("--reason")
    replay.add_argument("--before")
    replay.add_argument("--limit", type=int, default=100)
    replay.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = execute(args)
    except (RelayForgeError, ValueError, json.JSONDecodeError, OSError) as exc:
        _emit(
            {"error": type(exc).__name__, "message": _safe_error_message(exc)},
            stream=sys.stderr,
        )
        return 2
    except Exception as exc:  # pragma: no cover - defensive process boundary
        _emit({"error": "internal_error", "type": type(exc).__name__}, stream=sys.stderr)
        return 1
    _emit(result, stream=sys.stdout)
    return 0


def execute(args: argparse.Namespace) -> Any:
    store = Store(args.db)
    if args.command == "init":
        store.initialize()
        return {"initialized": True, "database": str(Path(args.db))}

    store.initialize()
    if args.command == "endpoint" and args.endpoint_command == "add":
        secret = os.environ.get(args.secret_env)
        if secret is None:
            raise ValidationError(f"environment variable {args.secret_env} is not set")
        endpoint = store.register_endpoint(
            tenant_id=args.tenant,
            url=args.url,
            secret=secret,
            max_in_flight=args.max_in_flight,
        )
        return {
            "id": endpoint.id,
            "tenant_id": endpoint.tenant_id,
            "url": endpoint.url,
            "max_in_flight": endpoint.max_in_flight,
        }

    if args.command == "event" and args.event_command == "enqueue":
        payload_text = sys.stdin.read() if args.payload == "-" else Path(args.payload).read_text(encoding="utf-8")
        payload = json.loads(payload_text)
        event, created = store.enqueue_event(
            tenant_id=args.tenant,
            topic=args.topic,
            payload=payload,
            idempotency_key=args.key,
        )
        return {"id": event.id, "created": created}

    if args.command == "worker" and args.worker_command == "run-once":
        engine = DeliveryEngine(
            store=store,
            transport=UrllibTransport(),
            config=ServiceConfig(default_batch_size=args.limit),
        )
        return engine.run_once(worker_id=args.worker, limit=args.limit).as_dict()

    if args.command == "delivery" and args.delivery_command == "inspect":
        return store.inspect_delivery(args.delivery_id)

    if args.command == "dead-letter" and args.dead_command == "list":
        return store.list_dead_letters(tenant_id=args.tenant, limit=args.limit)

    if args.command == "dead-letter" and args.dead_command == "replay":
        before = from_iso(args.before) if args.before else None
        ids = store.replay_dead_letters(
            now=utc_now(),
            tenant_id=args.tenant,
            reason=args.reason,
            before=before,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        return {"dry_run": args.dry_run, "count": len(ids), "delivery_ids": ids}

    raise ValidationError("unsupported command")


def _emit(value: Any, *, stream: Any) -> None:
    print(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True),
        file=stream,
    )


def _safe_error_message(exc: BaseException) -> str:
    message = str(exc)
    return message[:300]


def _display_url(url: str) -> str:
    from .redaction import redact_url

    return redact_url(url)


if __name__ == "__main__":
    raise SystemExit(main())
