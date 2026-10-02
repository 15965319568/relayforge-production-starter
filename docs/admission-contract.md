# Secret, rate, circuit, and worker contract

## Secret rotation

Each endpoint has monotonically increasing versions and stable key IDs (`v1`,
`v2`, ...). A rotation makes the new version the only signing key immediately.
The previous key remains available for verification while `now < retired_at`;
it is invalid at the exact retirement instant.

The v2 signature input is:

```text
key_id + "." + timestamp + "." + event_id + "." + delivery_id + "." + body
```

using HMAC-SHA256 and prefix `v2=`. Verification uses constant-time comparison.

## Persistent rate limit

The tenant token bucket stores rate, burst, token balance, and update time in
SQLite. Refill is based on elapsed UTC time, never mints tokens for a backwards
clock, and caps at burst. Reservation is atomic across processes. A denied
reservation does not consume tokens and reports the delay until the requested
amount is available.

## Persistent circuit

Closed circuits count failures. Reaching the configured threshold opens the
circuit until an exact UTC deadline. Before that deadline requests are denied.
At the deadline one caller obtains a half-open probe token; concurrent callers
are denied. Only that token may close or reopen the half-open circuit. Probe
failure starts a full new cooldown; success clears the failure count.

## Worker generations and lease renewal

Registering an existing worker ID creates a fresh opaque generation and fences
the old process. Heartbeat and drain changes require that exact generation.
Stale-worker selection includes a heartbeat exactly at the cutoff and is
ordered by heartbeat then worker ID.

The lease renewal coordinator renews each tracked delivery independently. A
lost token is removed and reported without preventing other leases from being
renewed.

