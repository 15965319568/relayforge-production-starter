# Architecture

RelayForge has four deliberately separate layers:

1. `Store` owns SQLite schema and transactions. It returns immutable domain
   records rather than live cursors.
2. `FairScheduler` chooses which due candidates should consume a batch. It
   does not mutate persistence.
3. `DeliveryEngine` coordinates leases, request construction, transport,
   outcome classification, retries, metrics, and audit logging.
4. `cli` is a stable JSON interface used by automation.

## Delivery lifecycle

```text
pending ----claim----> in_flight ----2xx----> succeeded
   ^                       |
   |                       +----retryable----> retry_wait
   |                       |
   |                       +----terminal/exhausted----> dead
   |                                                    |
   +-------------------------- replay ------------------+
```

An expired `in_flight` lease becomes eligible for recovery. Recovery does not
erase an attempt: an unfinished attempt is marked `abandoned`, then the next
worker receives a new monotonically increasing attempt ordinal and lease token.

Delivery is **at least once**. A process can crash after the endpoint accepted
a request and before SQLite records success. Consumers use the stable
`Idempotency-Key` header (the delivery ID) to deduplicate such calls.

## Ownership boundaries

- Only `Store` opens SQLite transactions.
- Only `DeliveryEngine` classifies transport outcomes.
- Only `RetryPolicy` calculates a next eligible time.
- Secrets may enter `signing`, but must not enter logs, CLI output, attempt
  response excerpts, or exception messages.

## Compatibility

This service runs on Python 3.11+ and SQLite 3.35+. Runtime code may use only
the Python standard library. Keep the synchronous API: operators embed it in
small cron workers and existing callers are not async-aware.

