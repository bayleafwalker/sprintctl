# Shared-work and legacy PostgreSQL recovery

`SPRINTCTL_BACKEND=remote` is retired. Normal Sprintctl client resolution
accepts only these modes:

| Mode | Use |
| --- | --- |
| `local` (ordinary default) | A repository-local SQLite database. |
| `served` | Shared work through the Vuoro service catalog. |

A stale `SPRINTCTL_BACKEND=remote` or `.sprintctl/backend.json` marker with
`"backend": "remote"` fails before Sprintctl imports `psycopg` or opens
PostgreSQL. `SPRINTCTL_URL` **on its own does not select a backend**. A resolved local
client ignores it; a served client refuses it. The stable failure message
names the migration path; it is intentionally a circuit breaker rather than a
best-effort fallback.

## Shared work: use served mode

Install `sprintctl[served]` with Python 3.12 or newer. Use a validated Vuoro
profile and no PostgreSQL URL:

```sh
export SPRINTCTL_BACKEND=served
export SPRINTCTL_VUORO_PROFILE=/path/to/vuoro-profile.json
unset SPRINTCTL_URL
sprintctl usage --context --json
```

An explicit profile takes precedence over a protected adapter handoff. Without
an explicit backend or profile, a served marker can select served mode only
when the exact worktree has a valid protected delegated handoff. An invalid
handoff refuses resolution; it does not authorize a local fallback. A marker
alone does not select served mode. See the
[delegated workspace contract](../reference/vuoro-work-adapter.md).

`SPRINTCTL_BACKEND=served` combined with `SPRINTCTL_URL` is refused. The
repository marker must likewise name `served` and retain its immutable
`repo_id`. `sprintctl doctor` validates the profile and catalog using the
served client; it does not probe a legacy database. A server catalog gap fails
as `served-operation-unavailable` rather than falling back to PostgreSQL.

For exact repository selection, current coordinator operations and the
remaining project workflow gaps, see [served coordination](served-coordination.md).

## Local recovery

For a standalone local database, work from a separate directory with no served
or remote repository marker in its ancestry, or from an established local
repository. Set local mode explicitly and select the intended SQLite file:

```sh
cd /path/to/marker-free-recovery-directory
export SPRINTCTL_BACKEND=local
unset SPRINTCTL_URL SPRINTCTL_VUORO_PROFILE
export SPRINTCTL_DB=/path/to/recovery.db
sprintctl doctor
```

`SPRINTCTL_DB` selects the SQLite file; it does not override backend selection
or a repository marker. Unsetting the backend inside a protected delegated
worktree can select served mode, while selecting local mode in a served
repository is refused. Do not edit the shared marker to force recovery.

Do not change a repository marker to `served` until the matching profile,
backfill/cutover evidence, and operator approval exist. A marker is a
repository identity contract, not a shortcut around those prerequisites.

## Command availability and current work

A command appearing in `--help` does not establish that its served operation is
available or authorized. `maintain check [--sprint-id N] [--json]` invokes the
existing read-only `work.maintain.check` operation. With no active sprint it
returns an empty diagnostic; an unknown explicit sprint remains an error. Its
thresholds come from server policy; `--threshold` is local-only.

The CLI routes `maintain sweep`, `maintain carryover`, `sprint backlog-seed` and
`render` as unavailable in served mode. They
fail with `served-operation-unavailable`; do not switch a shared checkout to a
local database to work around that refusal.

For local work, `maintain check --sprint-id N --json` is read-only. It has no
`--fix` option. `maintain sweep --sprint-id N` mutates stale item status and can
also close sprints when `--auto-close` is explicitly supplied. It also interrupts
eligible stale reservations across the selected database: that reservation sweep
has no sprint filter. Run it only when those changes are intended. A local `render` is a derived snapshot of the selected
local authority, never a substitute for a shared served snapshot.

Advisory reservations record coordination, not permission or claim proof.
Served item and sprint status commands carry their immutable basis through the
owner lifecycle operation; omit `--expected-revision` and `--actor` overrides.
Direct local status commands instead require the current `status_revision`
through `--expected-revision`. Hosted protected work claims and their heartbeats
are a separate protocol, not a prerequisite for ordinary CLI reservations or
status changes. See [reservation discipline](../advanced/reservation-discipline.md).

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
- [Reservation discipline](../advanced/reservation-discipline.md)
