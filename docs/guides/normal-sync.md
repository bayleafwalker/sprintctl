# Normal synchronization

Sprintctl keeps a durable, repository-local producer outbox and a read-only
projection cache beneath `.sprintctl`:

- `sync-outbox.db` holds locally authored observations and explicit authority
  requests.
- `sync-projection.db` caches remote observations and authority decisions.

Observations append to the outbox as normal work memory; no rollout flag is
needed. Run synchronization against a configured served backend to upload a
bounded batch and atomically advance both cached watermarks:

```bash
sprintctl sync --batch-size 100 --json
```

The command is safe to retry. Remote ingest deduplicates producer stream
records, so an interrupted response cannot create a second observation.
Authority requests remain durable when their outcome is unknown. Normal sync
may pull a decision that already exists, but it never originates or retries an
authority effect; use the explicit authority reconciliation command to do so.

## Upgrading from v0.2

On the first normal append or synchronization, Sprintctl copies a legacy
`shadow-pilot-outbox.db` or `shadow-pilot-projection.db` into the normal
locations if those locations do not already exist. The legacy files remain
untouched so that a retained v0.2 backup can be used for rollback. Once the
normal files exist they always win, making the migration idempotent.

Projection-backed reads remain guarded by `projection-reads`. They explicitly
fall back to the authoritative backend when the normal cache is absent, stale,
or incompatible.
