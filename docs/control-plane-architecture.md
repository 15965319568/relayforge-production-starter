# Enterprise control-plane architecture

RelayForge's data plane remains the durable delivery engine documented in
`architecture.md`. The enterprise control plane shares the same SQLite file and
adds routing, admission, operational workflows, and a management surface.

## Components

- `ControlDatabase` installs control schema version 2, upgrades the frozen v1
  control schema, and backfills existing endpoints without changing delivery
  identity.
- `SubscriptionRepository` maps an accepted event to enabled endpoints using a
  topic pattern and a bounded JSON filter language.
- `SecretRing` rotates endpoint signing keys while retaining old verification
  keys for a configured overlap window.
- `PersistentTokenBucket` and `PersistentCircuitBreaker` coordinate admission
  across worker processes. Neither may rely on process-local state.
- `ReplayJobService` snapshots dead letters into an idempotent, resumable job.
- `AuditExporter` exposes redacted, tenant-scoped pages through signed opaque
  cursors.
- `RetentionService` minimizes old response/audit data without erasing active,
  failed, or migration evidence.
- `WorkerRegistry` fences restarted worker identities with a generation token.
- `ManagementApp` and `relayforge-admin` expose the same service contracts to
  HTTP automation and operators.

## Transaction boundaries

Every persisted control decision uses its own SQLite connection and
`BEGIN IMMEDIATE`. A service can return a shorter result under contention but
must not overspend a token bucket, admit multiple half-open probes, run one
replay item twice, or accept an old worker generation.

Schema creation, v1-to-v2 migration, and backfill run in one control write
transaction after the delivery schema is initialized. Unknown versions are
rejected without mutation.

## Security boundary

Endpoint secrets and cursor-signing secrets may be read only by the component
that needs them. They never appear in dataclass representations returned to an
operator, API payloads, CLI output, diagnostics, audit exports, or exception
messages. Management requests name environment variables rather than carrying
secret values in JSON or command arguments.

