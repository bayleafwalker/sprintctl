# Shared-work and legacy PostgreSQL recovery

`SPRINTCTL_BACKEND=remote` is retired. Normal Sprintctl client resolution
accepts only these modes:

| Mode | Use |
| --- | --- |
| `local` (default) | A repository-local SQLite database. |
| `served` | Shared work through the Vuoro service catalog. |

A stale `SPRINTCTL_BACKEND=remote`, `SPRINTCTL_URL`, or
`.sprintctl/backend.json` marker with `"backend": "remote"` fails before
Sprintctl imports `psycopg` or opens PostgreSQL. The stable failure message
names the migration path; it is intentionally a circuit breaker rather than a
best-effort fallback.

## Shared work: use served mode

Use a validated Vuoro profile and no PostgreSQL URL:

```sh
export SPRINTCTL_BACKEND=served
export SPRINTCTL_VUORO_PROFILE=/path/to/vuoro-profile.json
unset SPRINTCTL_URL
sprintctl usage --context --json
```

The repository marker must likewise name `served` and retain its immutable
`repo_id`. `sprintctl doctor` validates the profile and catalog using the
served client; it does not probe a legacy database. A server catalog gap fails
as `served-operation-unavailable` rather than falling back to PostgreSQL.

## Local recovery

For a standalone local database, remove stale direct-remote environment
variables, point `SPRINTCTL_DB` at the intended SQLite file if necessary, and
use the normal local commands:

```sh
unset SPRINTCTL_BACKEND SPRINTCTL_URL
export SPRINTCTL_DB=/path/to/recovery.db
sprintctl doctor
```

Do not change a repository marker to `served` until the matching profile,
backfill/cutover evidence, and operator approval exist. A marker is a
repository identity contract, not a shortcut around those prerequisites.

## Explicit legacy administration only

The following named operator utilities may still use a direct PostgreSQL URL
because they are migration, schema-owner, or point-in-time recovery tools, not
normal work clients:

- `sprintctl remote-schema check|migrate|stage-maintenance-bridge`
- `sprintctl migrate-to-remote`
- `sprintctl remote-backfill`
- `SPRINTCTL_BACKEND=remote sprintctl db recover-from-remote --output recovery.db --verify`

Treat these as explicitly authorized operational procedures. They are not a
supported way to resume claims, read work, or mutate shared sprint state.
`db recover-from-remote` is read-only against PostgreSQL and writes a new local
SQLite authority: active claims are closed and all claim tokens are stripped.

## Operator checklist

- [ ] Shared sessions use `SPRINTCTL_BACKEND=served` and a validated profile.
- [ ] `SPRINTCTL_URL` is absent from normal developer and agent environments.
- [ ] Legacy `remote` markers are changed only under a documented, approved
  served cutover.
- [ ] Direct PostgreSQL credentials are supplied only to a named owner or
  recovery operation, never to normal Sprintctl dispatch.

## Related

- [Vuoro served-authority alignment](../plans/vuoro-served-authority-alignment.md)
- [#1164 gate-evidence ledger](../plans/1164-gate-evidence-ledger.md)
- [Claim discipline](../advanced/claim-discipline.md)
