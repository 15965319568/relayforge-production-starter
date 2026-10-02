# RelayForge production architecture

RelayForge is a multi-tenant webhook relay whose durable coordination boundary is one
SQLite database. The runtime is intentionally deployable as several independent OS
processes. In-process locks may protect local collections, but they are never the
authority for ownership, admission, repair, or replay.

## Request and delivery flow

An accepted event snapshots its endpoint fan-out. A worker lists due deliveries,
applies deterministic tenant/endpoint fairness, and atomically claims a bounded set.
The claim creates a short-lived fencing lease. Before network I/O, production
admission evaluates the tenant rate policy, tenant and endpoint capacity, and the
endpoint circuit. A denial releases the claim into `retry_wait` without creating an
attempt. An allowance reserves one token and, when necessary, owns the half-open
probe.

The delivery processor starts exactly one attempt for the lease, selects the current
secret version, creates a v2 signature over the exact body, and performs the HTTP
exchange. Delivery state, attempt outcome, admission state, dead-letter state, and
circuit feedback are committed together. HTTP 4xx terminal responses prove that the
endpoint was reachable and do not count as transport failures. Transport errors and
HTTP 5xx responses do.

The HTTP exchange itself cannot be transactional. If a process exits after the
receiver accepts a request but before local commit, the lease expires and another
worker retries with the same delivery idempotency key. The service therefore offers
at-least-once delivery, not exactly-once delivery.

## Process lifecycle

A worker registration has a random generation. Re-registering the same worker ID
fences the previous process. A running worker heartbeats its generation, tracks every
claimed lease, and renews active leases while request futures run. Drain first marks
the worker as draining and stops new claims; active requests may finish until the
shutdown deadline. Work still active at process termination is recovered only after
its persisted lease expires.

## Management and authentication

`ManagementApp` and `relayforge-admin` compose the same domain services. Bearer
credentials store PBKDF2 hashes, have explicit scopes, and are either bound to one
tenant or are unscoped operator credentials. Resource tenant ownership is resolved
before the application handler runs. `/live` and `/ready` are probe routes and do not
require a bearer credential. Every other management route does.

`/live` performs only a SQLite read. `/ready` checks the read/write path, all three
schema versions, stale non-draining workers, and unfinished migrations. Request IDs
are accepted when bounded or generated at the boundary and returned in the response.

## Maintenance and recovery

Consistency scanning is read-only with respect to business state. It persists an
ordered finding snapshot and a SHA-256 fingerprint. A repair plan names a completed
scan and snapshots its safe actions. Apply recomputes the fingerprint inside the
write transaction; any state drift makes the plan stale. Action preconditions use
entity versions where available, and all actions in one plan commit or roll back
together.

Online backup uses SQLite's backup API so WAL state is included consistently. A
temporary database is closed and flushed, checked with `integrity_check` and
`foreign_key_check`, hashed, and atomically published. Restore verifies the sidecar
manifest and validates into a new path. Existing targets are never overwritten.

## Trust boundaries

Payloads, endpoint URL query/userinfo, secrets, signatures, bearer credentials,
lease tokens, probe tokens, cursor secrets, and worker metadata are sensitive.
Operational metrics use bounded labels and must not include entity identifiers.
Structured logging applies the same header and URL redaction rules as audit export.
Synthetic fixtures use reserved `.test` hosts and obvious fixture credentials.
