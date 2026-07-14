# Shadow projection pilot

The shadow pilot is an opt-in migration aid for the outbox ADR. It records
selected observations twice: the existing sprintctl event store remains the
only authority, and a separate producer outbox retains an observation-only
transport copy for comparison and synchronization.

It never changes claims, item status, sprint status, or any other authority
path. Disable it to stop future shadow writes; neither disabling nor a pilot
failure changes existing sprintctl data.

## Enable and inspect

Run these commands from the repository root. The configuration and local pilot
databases live under the gitignored `.sprintctl/` directory.

```sh
sprintctl pilot status --json
sprintctl pilot enable
sprintctl pilot status --json
```

The default is disabled. `pilot enable` is a per-repository, explicit opt-in;
paths are fixed below `.sprintctl/` and cannot be redirected by configuration.

## What is mirrored

Only event types classified as observations by the outbox contract are
eligible. At present these include `note.recorded`, `decision.recorded`,
`work.completed`, and `doc-ref.added`. `sprintctl event add` commits its normal
authority event first, then mirrors an eligible observation using a stable ID
derived from the authoritative event identity. Retrying the mirror does not
allocate another producer sequence.

Authority commands, remote decisions, and unclassified generic events are not
mirrored. A shadow error is reported to the command result but never rolls back
an already committed authority event.

## Compare and synchronize

```sh
sprintctl pilot verify --sprint-id 406 --json
sprintctl pilot sync --batch-size 100 --json
```

`verify` compares the current authoritative observation history for the sprint
with the local producer outbox and reports `equal`, `missing`, `unexpected`,
and `mismatched` evidence. It is read-only.

`sync` requires the repository's existing remote backend. It uploads only the
producer's observations, relies on remote ingest deduplication for safe retry,
and atomically advances the local cached-projection watermark. The cache is a
read side, not authority.

## Rollback

```sh
sprintctl pilot disable
```

This stops new mirrors immediately. Existing SQLite/PostgreSQL behavior is
unchanged, and the local pilot outbox/projection can be retained for evidence
or removed as local operational state after the pilot is no longer needed.
