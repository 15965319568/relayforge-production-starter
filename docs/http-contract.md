# Outbound HTTP contract

The transport receives a `POST` request with the endpoint URL, exact canonical
JSON body bytes, and these headers:

- `Content-Type: application/json`
- `X-Relay-Event-Id: <event id>`
- `X-Relay-Delivery-Id: <delivery id>`
- `X-Relay-Timestamp: <UTC Unix seconds>`
- `X-Relay-Signature: v1=<lowercase SHA-256 HMAC hex>`
- `Idempotency-Key: <delivery id>`
- `User-Agent: RelayForge/0.1`

Canonical JSON is UTF-8, sorted by object key, contains no insignificant
whitespace, preserves array order, and rejects NaN/infinity.

Signature input is the byte concatenation:

```text
timestamp + "." + event_id + "." + canonical_body
```

The endpoint secret is the HMAC-SHA256 key. Timestamps and signatures may
change between attempts; body bytes and idempotency key must not.

Request/response audit records must treat header names case-insensitively.
`Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, and
`X-Relay-Signature` values are `[REDACTED]`. Logged URLs omit user information,
query strings, and fragments. Response excerpts are at most 256 Unicode code
points and replace ASCII control characters other than tab/newline with `?`.

