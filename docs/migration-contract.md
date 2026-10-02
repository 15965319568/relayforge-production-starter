# Legacy v0 migration contract

`fixtures/legacy_v0.sql` is the supported incident-era database shape. Calling
`Store.initialize()` on that database must upgrade it to current schema version
1 without requiring an export/import step.

## Atomicity and compatibility

- The migration runs under one SQLite write transaction. Any unsupported row,
  missing required v0 table, SQL error, or process exception leaves the complete
  v0 schema and data visible, with no `legacy_*` or partial v1 tables.
- Re-running `initialize()` after success is a no-op except for ensuring current
  indexes exist. Exactly one `schema_migration` audit row is written.
- Unknown versions other than 0 and 1 are rejected without mutation.
- IDs, endpoint configuration, event identity, delivery attempt counts, attempt
  ordinals, response excerpts, and dead-letter replay counters are preserved.
- Migration audit details contain row counts and version numbers only. They may
  not contain endpoint secrets, URL queries, payloads, or response bodies.

## State mapping

| v0 delivery status | v1 state | Additional rule |
| --- | --- | --- |
| `queued` | `pending` | `available_at` becomes `next_attempt_at` |
| `processing` | `retry_wait` | clear old lock; `locked_until` becomes due time |
| `retrying` | `retry_wait` | preserve `available_at` |
| `delivered` | `succeeded` | preserve finish time and status |
| `failed` | `dead` | preserve or reconstruct the dead-letter audit row |

An unfinished v0 attempt becomes `abandoned`, using its start time as the
finish time when no better timestamp exists. Its error kind is
`migration_recovered` if the old row had no error. Completed results map as:
`success -> succeeded`, `retry -> retryable`, and `failure -> terminal`.

## Data normalization

- Legacy timestamps may be `Z`, offset-aware ISO-8601, contain microseconds, or
  be naive UTC. Store them as UTC `Z` strings without microseconds.
- Parse and re-encode event JSON using the same canonical form as new events so
  idempotent re-enqueue after migration returns the original event.
- Legacy attempts receive deterministic internal lease tokens that are not
  exposed through inspection or audit output.
- After migration, `PRAGMA foreign_key_check` returns no rows. A recovered
  retry can be claimed and its next attempt ordinal continues monotonically.

