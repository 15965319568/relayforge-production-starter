from __future__ import annotations

import argparse
import json
from pathlib import Path

from relayforge.store import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    store = Store(args.db)
    store.initialize()
    endpoints = json.loads((root / "fixtures" / "endpoints.json").read_text(encoding="utf-8"))
    for endpoint in endpoints:
        endpoint_args = dict(endpoint)
        endpoint_args["endpoint_id"] = endpoint_args.pop("id")
        store.register_endpoint(**endpoint_args)
    with (root / "fixtures" / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            payload = event.pop("payload")
            event["event_id"] = event.pop("id")
            store.enqueue_event(payload=payload, **event)
    print(json.dumps({"seeded": True, "database": args.db}, sort_keys=True))


if __name__ == "__main__":
    main()
