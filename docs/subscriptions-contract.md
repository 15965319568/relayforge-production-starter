# Subscription and filter contract

## Topics and patterns

Topics contain 1 to 12 lowercase dot-separated segments. Each segment begins
with `[a-z]` and then contains up to 63 lowercase letters, digits, `_`, or `-`.

Patterns use the same literals plus:

- `*` matches exactly one segment;
- `#` matches zero or more trailing segments and is valid only as the final
  segment.

Pattern evaluation is deterministic and case-sensitive. Invalid topics or
patterns are operator/input errors.

## Filter language

A filter is a JSON object. `{}` matches every payload. Field names are dot paths
through JSON objects and distinguish a missing field from a field whose value
is `null`.

Supported field operators are `$eq`, `$ne`, `$in`, `$exists`, `$prefix`, `$gte`,
and `$lte`. Logical operators are `$and`, `$or`, and `$not`. Ordered comparison
does not coerce strings, numbers, or booleans. Filters are limited to 8 levels,
64 terms, and 64 values per `$in` expression.

Stored filters use canonical JSON. Equivalent key order therefore participates
in subscription uniqueness and idempotency consistently.

## Routing

Only enabled subscriptions attached to enabled endpoints in the same tenant
participate. Multiple matching subscriptions for one endpoint produce one
delivery. Order is stable by subscription creation and ID. Fan-out is
snapshotted when an event is accepted; later subscription changes do not alter
that event.

Reusing an event idempotency key with semantically identical content returns
the original event and original routed endpoint set. Reusing it with a changed
topic or payload is a conflict.

When control schema is first installed on an existing delivery database, each
endpoint receives one enabled `#` subscription so legacy fan-out behavior is
preserved until operators replace it.

