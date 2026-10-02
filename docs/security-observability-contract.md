# Security and observability contract

## Credentials and authorization

Bearer credentials are returned only at issue time. Persistent rows contain a random
salt and PBKDF2-HMAC-SHA256 digest. Authentication errors do not reveal whether an ID
exists, and revocation takes effect on the next request.

Tenant credentials require both the route scope and an exact resource/request tenant.
They cannot perform an all-tenant operation by omitting `tenant_id`. Operator
credentials are unscoped and cannot be issued with a tenant. Resource lookup failure
is 404 for an authorized operator; cross-tenant access is 403. Missing or invalid
authentication is 401; malformed input is 400; state conflict is 409.

The middleware may inspect a bounded JSON body for authorization but must restore the
same bytes for the application. Request bodies larger than the configured bound are
rejected before reading or parsing. `/live` and `/ready` are the only unauthenticated
routes.

## Metrics

Metric definitions declare their label names before use. Tenant, endpoint, delivery,
event, request, credential, worker, and token identifiers are prohibited labels.
The registry has a hard series bound. Counters are monotonic and histograms use fixed
cumulative buckets. Recommended labels are small enums such as outcome, reason,
decision, operation, and priority.

## Logs and management output

Structured records include event name, UTC timestamp, and correlation ID. Fields
named token, secret, password, authorization, cookie, payload, or body are redacted.
Headers use case-insensitive redaction; URLs omit userinfo, query, and fragment.
Unexpected errors expose a stable error class, not exception text.

Health, metrics, diagnostics, audit, backup manifests, CLI output, and API responses
must never expose payload content, raw credentials, signing secrets, signatures,
lease/probe/generation tokens, cursor secrets, or endpoint query values.
