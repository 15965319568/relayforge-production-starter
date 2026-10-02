# CLI contract

Every successful command prints exactly one JSON value to stdout. Expected
operator errors print one JSON object to stderr and return exit code `2`.
Unexpected internal errors return `1`. Secrets and raw endpoint query strings
must never be printed.

Commands:

```text
relayforge --db PATH init
relayforge --db PATH endpoint add --tenant T --url URL --secret-env NAME [--max-in-flight N]
relayforge --db PATH event enqueue --tenant T --topic TOPIC --key KEY --payload FILE
relayforge --db PATH worker run-once --worker ID [--limit N]
relayforge --db PATH delivery inspect DELIVERY_ID
relayforge --db PATH dead-letter list [--tenant T] [--limit N]
relayforge --db PATH dead-letter replay [--tenant T] [--reason R] [--before UTC] [--limit N] [--dry-run]
```

`endpoint add` reads the secret from the named environment variable. The
secret is stored but never echoed. `event enqueue --payload -` reads stdin.
`worker run-once` uses the standard-library HTTP transport in normal use; tests
invoke the engine directly with a fake transport.

`dead-letter replay --dry-run` returns the IDs that would be replayed without
changing the database. Replay ordering is `(dead_at, delivery_id)`.
`--before` accepts only the persisted UTC form ending in `Z`, for example
`2026-09-20T12:00:00Z`. Date-only, naive datetime, and numeric-offset forms are
operator errors and return exit code `2`.
