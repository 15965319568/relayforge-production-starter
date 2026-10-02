# Persistence and concurrency contract

## Event idempotency

`(tenant_id, idempotency_key)` identifies one logical event.

- Repeating the same topic and semantically identical JSON payload returns the
  original event and must not create more deliveries.
- Reusing the key with different content raises `IdempotencyConflict`.
- Endpoint fan-out is snapshotted when the event is first accepted. Registering
  another endpoint later does not alter the original event.

## Claiming

`claim_due` is a two-phase operation from the caller's point of view: select a
fair candidate set and atomically fence those exact rows. Persistently it must
behave as one claim transaction. A row is claimable when:

- state is `pending` or `retry_wait` and `next_attempt_at <= now`; or
- state is `in_flight` and `lease_expires_at <= now`.

Every successful claim writes a fresh opaque lease token. The token, not the
worker name, is the fencing identity. Finalization and lease renewal require a
matching, unexpired token. At the exact expiry instant the old token is stale.

Concurrent claimers may return fewer rows than requested. They must never
return the same delivery/token pair or raise a routine `database is locked`
error after normal busy-timeout retries.

Endpoint and tenant in-flight limits are global across workers sharing the
database, not advisory limits for one scheduler snapshot. Concurrent workers
claiming different deliveries must not make the number of unexpired leases
exceed either limit. Expired leases do not consume capacity. Returning fewer
rows than requested is the expected behavior when another worker consumed the
remaining capacity.

## Attempts

Attempt ordinals start at 1 and are unique per delivery. Starting an attempt is
idempotent for the same active lease token. A recovered unfinished attempt is
marked `abandoned` with a finish timestamp before a new attempt begins.

All timestamps are UTC ISO-8601 strings ending in `Z`, with microseconds
removed. Lexical and chronological ordering are therefore identical.

## Replay

Replaying a dead delivery resets it to `pending`, clears lease/error scheduling
fields, and preserves attempt history. `dead_letters.replay_count` increases
and the dead-letter row remains as audit history. The same delivery can become
dead and be replayed repeatedly.
