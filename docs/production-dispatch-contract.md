# Production dispatch contract

## Admission

- Admission requires a live `in_flight` delivery and exact lease token. Exact lease
  expiry is stale.
- Repeating admission for the same live lease is idempotent and does not consume a
  second token or create another probe.
- Rate refill never uses negative elapsed time and never exceeds burst.
- A denied rate or circuit decision moves the delivery to `retry_wait`, clears the
  lease, records a released admission, and creates no attempt.
- Concurrent connections cannot spend the same token or own the same half-open probe.
- A circuit denial does not consume a tenant token.
- Tenant capacity is globally enforced from persisted state. A batch that exceeds a
  newly tightened policy deterministically admits no more than the configured count.

## Attempt and finalization

- `start_attempt` is idempotent for a lease and ordinals remain monotonic after crash
  recovery.
- A current v2 secret is selected at request time. The signature binds key ID,
  timestamp, event ID, delivery ID, and exact canonical body.
- A 2xx response succeeds. 408, 425, 429, and 5xx are retryable. Other responses are
  terminal.
- `Retry-After` is case-insensitive, accepts non-negative seconds or HTTP-date, is
  bounded by policy, and is a lower bound on deterministic retry backoff.
- Successful exchanges and terminal HTTP 4xx close/reset the connectivity circuit.
  Transport failures and 5xx responses advance it.
- Attempt, delivery, dead letter, admission, and circuit result are one SQLite commit.
  A stale lease or stale probe commits none of them.
- Exception type may be persisted as a bounded category. Exception messages are not
  persisted because transports frequently embed credentials and URLs.

## Runtime

- Parallelism uses threads because the transport is blocking and SQLite remains the
  cross-process authority.
- Renewal failure for one lease does not prevent renewal of others.
- Drain stops claims before waiting for active work. Shutdown timeout does not erase
  leases or force a result write by a thread that no longer owns its lease.
- Worker generation, delivery lease token, and circuit probe token are independent
  fencing domains and are never interchangeable.
