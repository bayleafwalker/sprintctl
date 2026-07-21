---
doc_id: sprintctl-vuoro-served-authority-alignment
status: ratified
ratified_at: 2026-07-21
ratified_by: operator
governing_decision: agentops/docs/plans/agentops/vuoro-served-substrate-plan.md
---

# Sprintctl alignment with the Vuoro served authority

## Boundary

Sprintctl continues to own work, sprint and item lifecycle, claims, evidence,
authority-command arbitration, project batching, and work projection semantics.
The ratified Vuoro direction changes where those semantics execute: shared
authority is served through the Vuoro work adapter rather than imported into
every workstation installation.

`docs/plans/adr-outbox-sync-model.md` remains canonical for observations,
authority commands, remote decisions, identities, cursors, and disconnected
behaviour. This note supersedes only its implication that a direct database
client is the target delivery mechanism.

## Required changes

1. Extract Click-independent application handlers for work reads, claims,
   transitions, evidence, batching, and cutover evidence.
2. Publish a Vuoro work adapter that registers domain-qualified operations and
   JSON Schemas without duplicating sprintctl rules in the service shell.
3. Replace remote `_pg.init_db()` during normal command startup with a
   read-only compatibility check.
4. Package shared-authority migrations for an appservice-controlled job using
   a migration role. The service runtime role has no DDL.
5. Add handshake data for work API/schema compatibility and fail closed before
   serving an unsupported combination.
6. Move workstation remote profiles from `SPRINTCTL_URL` to Vuoro endpoint and
   identity. Keep self-migration only for explicitly local or marked recovery
   SQLite authorities.

`work.pilot.cutover-evidence` is the acceptance exhibit: a protocol-compatible
client installed before that operation is deployed must discover and invoke it
from the server catalog without reinstalling sprintctl.

## Migration sequence

1. Freeze new shared-schema changes until the migration job and role split are
   ready.
2. Ship a compatibility release that removes remote DDL and can use the Vuoro
   endpoint while retaining explicit local mode.
3. Deploy the migration job, work adapter, service handshake, and catalog in
   isolated `vuoro-dev`.
4. Prove parity, role isolation, stale-client behaviour, and rollback.
5. Remove shared database credentials from workstation identities.
6. Refine and execute backend retirement item #1164 only after catalog parity,
   recovery, and production promotion evidence exist.

## Non-goals

- moving sprintctl state ownership into agentops or the Vuoro shell;
- optimistic offline claims;
- cross-domain transactions;
- removing local SQLite authority from standalone/recovery use;
- treating package version strings as protocol compatibility.

## Verification

- normal service/client paths cannot execute DDL;
- migration jobs are idempotent and serialized;
- service startup refuses incompatible schemas without mutation;
- old direct clients fail explicitly after role/credential removal;
- catalog invocation and the legacy CLI produce equivalent accepted/rejected
  histories while the compatibility path exists;
- a pre-existing generic client invokes a newly deployed work operation.

## Backlog registration

- **#1193** — deployment-owned shared-schema migration and compatibility gate.
- **#1194** — sprintctl application core and Vuoro work adapter/catalog.
- **#1195** — endpoint/identity workstation cutover; blocked by #1193/#1194.
- **#1164** — P3 retirement gate; now also blocked by #1195.

Done **#1163** remains the capability-distribution exhibit. Historical #912
remains local migration-safety scope and is not the shared-authority item.
