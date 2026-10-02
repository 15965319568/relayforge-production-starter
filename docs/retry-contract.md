# Retry contract

## Classification

- `200..299`: success.
- `408`, `425`, `429`, and `500..599`: retryable HTTP responses.
- timeout, DNS, connection, and other `TransportError` failures: retryable.
- every other HTTP status: terminal failure.

The configured `max_attempts` counts actual transport calls, including the
first call. A retry is also forbidden when its scheduled time would be later
than `first_attempt_at + max_elapsed_seconds`.

## Backoff

For attempt ordinal `n`, the unjittered delay is:

```text
min(cap_seconds, base_seconds * multiplier ** (n - 1))
```

Apply deterministic symmetric jitter in
`[delay * (1-jitter_ratio), delay * (1+jitter_ratio)]`. The sample must be
derived from SHA-256 of `delivery_id + ":" + attempt_ordinal`; Python's process-
randomized `hash()` is not stable enough. Round the result to the nearest
millisecond.

`Retry-After` accepts either non-negative decimal seconds or an RFC 7231 HTTP
date. Invalid or past values are ignored. A valid hint is clamped to
`max_retry_after_seconds`, then used as a lower bound for the jittered policy
delay. The final delay is never negative.

## Defaults

| Setting | Default |
| --- | ---: |
| base seconds | 5 |
| multiplier | 2 |
| cap seconds | 3600 |
| jitter ratio | 0.20 |
| maximum attempts | 8 |
| maximum elapsed seconds | 86400 |
| maximum Retry-After seconds | 21600 |

