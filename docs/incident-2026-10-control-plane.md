# Incident RF-2026-10: control-plane rollout (sanitized)

## Summary

During a staged rollout of subscription routing and self-service replay, three
workers in separate processes shared one tenant shard. Delivery throughput
remained nominal, but operators reported missing filtered events, short bursts
well above tenant policy, repeated replay counters, rejected signatures after
rotation, and audit pages that crossed tenant filters when a saved cursor was
reused.

The timeline and exports under `fixtures/control_incident/` are synthetic and
contain no customer data. Identifiers join across files and preserve the
ordering, partial records, duplicate requests, and clock boundaries needed to
reproduce the incident shape.

## Observations

- A single-segment subscription wildcard matched a three-segment suffix in one
  worker but not another.
- Two rate reservations made against the same tenant balance were both allowed
  at the burst boundary.
- Several endpoints admitted more than one half-open probe after a cooldown.
- A replay creation request was retried after an HTTP timeout and produced a
  second job with a different item snapshot.
- A key retired exactly at 09:15:00Z was still accepted at that instant.
- An audit cursor created for one tenant was accepted with another tenant's
  query parameters.
- A restarted worker continued heartbeating with its previous generation.
- A retention dry-run and the immediately following real run reported different
  counts even though no delivery changed between them.

These are observations, not root-cause conclusions. Use the repository,
contracts, fixtures, tests, and database behavior to determine what must change.

## Operational constraints

- The service tier remains SQLite and multiple OS processes; process-local
  locks are not coordination.
- Existing delivery IDs, attempts, audit history, and v1 subscription IDs must
  remain valid.
- Operators require deterministic, resumable workflows after CLI or HTTP
  retries.
- Receivers require a bounded key-overlap window during rotation.
- API and CLI output are treated as potentially customer-visible and must not
  contain secrets or reusable fencing material.

