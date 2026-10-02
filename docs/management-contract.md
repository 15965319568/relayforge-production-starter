# Management API and admin CLI contract

## Common behavior

The WSGI API and `relayforge-admin` use the same service layer. Successful CLI
commands print exactly one JSON value to stdout. Expected input/operator errors
print one JSON object to stderr and exit 2; unexpected errors exit 1 without
exception text. API responses are JSON with `Cache-Control: no-store`.

Secret values are referenced by environment-variable name. They are never
accepted in a JSON body or printed. UTC inputs use the persisted form ending in
`Z`; date-only, naive, and numeric-offset forms are invalid.

## API routes

```text
GET  /health
POST /v1/subscriptions
POST /v1/secrets/{endpoint_id}/rotate
GET  /v1/audit
POST /v1/replay-jobs
POST /v1/replay-jobs/{job_id}/run
POST /v1/workers/register
```

Replay creation requires `Idempotency-Key`. Validation errors use 400, missing
resources 404, idempotency/transition conflicts 409, and unexpected failures
500 with only an error category. Request bodies are bounded to 1 MiB.

## CLI commands

```text
relayforge-admin --db PATH init
relayforge-admin --db PATH diagnose
relayforge-admin --db PATH subscription add|list ...
relayforge-admin --db PATH secret rotate ...
relayforge-admin --db PATH rate set ...
relayforge-admin --db PATH audit export ...
relayforge-admin --db PATH replay-job create|run ...
relayforge-admin --db PATH worker register ...
relayforge-admin --db PATH retention run ...
```

Cursor signing uses the environment variable named by `--cursor-secret-env`.
Worker registration returns its generation but does not echo metadata. Audit
export returns `items` and `next_cursor`; callers may persist the cursor but may
not alter it.

