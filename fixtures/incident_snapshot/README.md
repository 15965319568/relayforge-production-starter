# RF-2026-09 incident snapshot

This directory is a hand-curated, fully synthetic reenactment of the incident.
It contains no customer records or production credentials. Identifiers are
opaque and domains use `example.net`, but timestamps, skew, partial records,
cardinality, retry patterns, and cross-file relationships are intentionally
kept production-shaped.

The artifacts were prepared for an offline reliability exercise:

- `tenant_backlog.csv`: a point-in-time operations export at 08:51 UTC.
- `worker_timeline.ndjson`: selected structured worker events around a stale
  lease race. Tokens are aliases, not usable lease values.
- `delivery_export.ndjson`: mixed delivery states from the affected shard.

These files provide investigative context. The executable contract remains in
`docs/*-contract.md` and the test suite.
