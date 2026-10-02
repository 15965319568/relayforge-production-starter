# Incident RF-2026-09 (redacted)

## Summary

Between 08:40 and 10:05 UTC, webhook latency increased from 4 seconds to 47
minutes. One enterprise tenant produced roughly 80% of the pending deliveries.
Workers were restarted twice during mitigation. Operators then saw duplicate
consumer-side mutations, even though RelayForge showed only one final success
for several affected deliveries.

## Selected observations

The values below are synthetic reproductions with the same shape as production
data. They are evidence, not a requested implementation. The corresponding
structured excerpts are under `fixtures/incident_snapshot/`; their identifiers
join across the backlog, worker, and delivery exports.

```text
08:43:12 worker-03 claimed dlv_01J8QAQ2H6... token=lease-A lease_until=08:43:42
08:43:44 worker-07 claimed dlv_01J8QAQ2H6... token=lease-B lease_until=08:44:14
08:43:47 worker-03 finalized dlv_01J8QAQ2H6... status=204
08:43:50 worker-07 finalized dlv_01J8QAQ2H6... status=204
```

The old worker's write was accepted after expiry and after a new token existed.
The attempt table contained one `succeeded` row and one permanently `started`
row.

```text
tenant=org_7d83f1 due=18742 active=8
tenant=org_c91a20 due=31    active=0 oldest_age=00:41:13
tenant=org_42be19 due=7     active=0 oldest_age=00:35:02
```

All 25 rows in repeated worker batches belonged to `org_7d83f1` until its older
backlog drained.

```text
status=429 retry-after="Sun, 20 Sep 2026 12:02:00 GMT" result=dead
status=429 retry-after="120" result=dead
```

Some request audit entries also contained `X-Relay-Signature`, URL userinfo,
and `?token=...`. Transport exception strings from a proxy library sometimes
included the proxy URL with credentials.

## Operational constraints

- Deployment uses several OS processes; process-local locks do not coordinate
  them.
- SQLite remains the supported store for this service tier.
- Receivers support idempotency keys, so the accepted delivery guarantee is
  at-least-once rather than exactly-once.
- Operators depend on attempt and dead-letter history during customer support;
  recovery cannot erase it.
- Five frozen v0 databases from the affected shard must be upgraded in place.
  A representative schema and data slice is `fixtures/legacy_v0.sql`; failed
  migration cannot leave the database in a mixed-schema state.
