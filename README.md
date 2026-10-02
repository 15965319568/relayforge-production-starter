# RelayForge

RelayForge is a dependency-free webhook delivery and enterprise control-plane
service used by the payments platform team. It stores events in SQLite, routes
subscriptions to tenant endpoints, signs requests, coordinates admission across
worker processes, retries transient failures, and exposes resumable operations
through CLI and WSGI management surfaces.

The repository remains runnable on a laptop while modeling the production
service as a multi-module system: several workers can share one database,
dispatch admission and circuit state are durable, work is protected by expiring
leases, endpoint calls are at-least once, consistency repair is fenced, and
online backup/restore is independently verifiable.

## Quick start

```bash
python -m venv .venv
python -m pip install -e .
python -m relayforge --db demo.sqlite3 init
python scripts/seed_demo.py --db demo.sqlite3
python -m relayforge --db demo.sqlite3 worker run-once --worker local-1
export RELAYFORGE_CURSOR_SECRET=local-fixture-secret
relayforge-admin --db demo.sqlite3 init
relayforge-admin --db demo.sqlite3 diagnose
python -m unittest discover -s tests -v
```

No network service is required for the test suite. Tests inject a deterministic
transport in place of real HTTP calls.

## Repository map

- `src/relayforge/`: service code.
- `tests/`: executable public contract and regression tests.
- `docs/`: data-plane, control-plane, production dispatch, recovery, security,
  and operations contracts.
- `fixtures/`: deterministic sample data, historical schemas, and sanitized
  incident-shaped exports.
- `scripts/`: local setup and verification helpers.
- `TASK.md`: engineering task to complete.

The task-specific requirements in `TASK.md` take precedence over descriptive
examples in this README. Public interfaces documented under `docs/` are part
of the compatibility contract.
