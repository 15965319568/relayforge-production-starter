# Consistency, repair, backup, and restore contract

## Scanner

The scanner reports, at minimum, orphan attempts, terminal deliveries retaining
leases, attempt-count drift, dead-letter mismatches, replay-counter drift,
subscription tenant mismatch, invalid current-secret cardinality, token-bucket range
violations, circuit/probe mismatch, and stale dispatch admissions.

Findings are ordered by stable business keys and serialize canonically. Running a
scan does not alter deliveries, attempts, dead letters, replay items, subscriptions,
secrets, buckets, circuits, or admissions. Persisting the scan record is expected.
Two scans over the same business state produce the same fingerprint even when their
run IDs and insertion histories differ.

## Repair plans

Only explicitly allowlisted, deterministic repairs may be planned. Ambiguous cases,
including cross-tenant subscription ownership and current-secret selection, require
operator investigation and are not auto-repaired.

A plan stores its source fingerprint and action payloads. Apply occurs in one
`BEGIN IMMEDIATE` transaction. Before the first action, the current finding
fingerprint must still match. Versioned rows must still match the expected version.
On mismatch the plan becomes `stale` and no action is applied. An applied, stale, or
cancelled plan cannot run again.

## Backup

- Destination and restore paths are explicit; an existing file is an error.
- Backup uses `sqlite3.Connection.backup`, not a byte copy of a live WAL database.
- Publication happens only after close/flush, integrity check, foreign-key check,
  schema-version validation, SHA-256, and size calculation.
- The JSON sidecar contains identifier, checksum, size, schema versions, and creation
  time. It contains no source database path or secrets.
- Restore verifies size and checksum before creating its target, then validates the
  copied database before atomic publication.
- Failure removes incomplete temporary files and never modifies an existing target.
