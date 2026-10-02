# Production recovery exercise fixture

This directory contains fully synthetic, production-shaped exports created for the
RelayForge recovery exercise. It is not customer telemetry. Hostnames use the
reserved `.test` suffix, identifiers are invented, payloads are omitted, credential
values are never included, and checksums identify generated fixture artifacts only.

The files share request, worker, delivery, endpoint, scan, repair, and backup IDs so
an investigator can reconstruct chronology without assuming that one export is the
source of truth. Timestamps are normalized to UTC `Z` form.

- `dispatch_admissions.csv`: joined dispatch/admission outcomes around the incident.
- `worker_timeline.ndjson`: registration, drain, renewal, and shutdown observations.
- `auth_requests.ndjson`: sanitized management authorization outcomes.
- `maintenance_runs.ndjson`: consistency scan, repair plan, and backup lifecycle.
- `backup_catalog.csv`: recovery catalog metadata without filesystem paths.
