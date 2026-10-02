# Replay, audit, retention, and diagnostics contract

## Replay jobs

Creating a replay job snapshots matching deliveries that are currently dead,
ordered by `(dead_at, delivery_id)`. The request can filter tenant, reason, and
an exclusive `before` timestamp. An idempotency key returns the original job
for the same normalized request and conflicts for a different request.

Items are processed in stable ordinal batches. Delivery reset, dead-letter
counter increment, item state, and job counters commit together. A delivery no
longer dead is marked skipped. New dead letters after job creation are not
absorbed. Re-running a completed job is a no-op; a cancelled job cannot run.

## Audit export

Pages are ordered by audit row ID and limited to 1..500 items. Filters include
tenant, kinds, inclusive `after`, and exclusive `before`. The opaque cursor is
HMAC authenticated, expires at an exact instant, and is bound to the scope and
normalized filter set. It cannot be reused for another tenant or query.

Every exported item is defense-in-depth redacted, including nested sensitive
headers and keys named `secret`, `lease_token`, `token`, or `signature`.
NDJSON contains one canonical object per line and a trailing newline.

## Retention

Retention can clear response excerpts and remove audit rows only for succeeded
deliveries older than their respective cutoffs. Dead, pending, retrying, and
in-flight evidence is retained. `schema_migration` audit rows are never removed.
Dry-run reports the exact planned counts and performs no mutation. A real run
records its counts and cutoffs without storing removed content.

## Diagnostics

The snapshot reports delivery state counts, tenant due/active/dead counts,
oldest due timestamps, non-closed circuit count, stale worker count, and replay
job states. It contains no endpoint URLs, payloads, secrets, signatures, lease
tokens, or worker metadata.

