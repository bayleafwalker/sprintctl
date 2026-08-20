# Sprintctl volatile-context native-hook pilot

Status: implementation pilot (not deployed)

Governing mapping: Agentops
`docs/plans/agentops/volatile-context-native-runtime-integration-mapping-2026-08-20.md`.
The imported bundle is historical input; its dispatcher binding and exclusive
claim assumptions are not implemented here.

## Current path inventory

The inventory below is against Sprintctl `origin/main` `15afc87` (v0.3.0).

| Concern | Current owner path | Revision/precondition | Pilot disposition |
| --- | --- | --- | --- |
| Direct item status | `commands/work.py` -> `db.set_work_item_status` | required `--expected-revision`; SQLite CAS | projected and recognized by the pilot |
| Served item status | `work.lifecycle.arbitrate` -> authority command | immutable command basis revision; owner arbitration and PostgreSQL row lock | projected and recognized by the pilot |
| Direct sprint status | `commands/work.py` -> `db.set_sprint_status` | required `--expected-revision` | unchanged; outside item pilot |
| Item description | direct `item edit` and served `work.item.edit` | description revision CAS; served precondition required | exposed by existing item read; outside status pilot |
| Item create, refs, deps, notes, events | direct CLI and matching served operations | creation or append/remove identity; no universal item-status precondition | not recognized by hook; no claim of coverage |
| Advisory reservations | direct `commands/reservation.py`; served `work.reservation.*` | authenticated attribution/row identity, not mutation authority | may be read elsewhere; never used as projection proof |
| Maintenance lifecycle | `maintenance_capability.py`, served maintenance operations | owner capability revision and request idempotency | unchanged; separate resource projection already exists |
| Maintenance/recovery commands | owner-specific CLI/application paths | operation-specific | unchanged; no hook interception |
| Direct item reads | `item show`, `item list`, `usage --context` | backend snapshot plus explicit cached-projection freshness disclosure where supported | unchanged |
| Served reads | `work.read.*` application operations | repository-scoped identity and application snapshots | adds bounded `work.read.item-projection` |
| Cached ingestion projection | `projection.py`, `sync.py`, guarded `projection_reads.py` | ingestion watermark; read fallback only | not reused as item authority or hook cursor |
| MCP | no Sprintctl-owned MCP mutation registration in this baseline | none assumed | only the explicit structured pilot tool names are recognizable; transport must still call the owner operation |
| Native hooks | no installed Sprintctl native hook on this baseline | n/a | adds opt-in `sprintctl-volatile-hook`; no settings are installed automatically |
| ActionQ | no call from Sprintctl item read/CAS paths | federation contract pending | no dependency or fabricated execution binding |

No write path is silently classified as covered. The pilot recognizes only
the structured `sprintctl.item_status` and
`mcp__sprintctl__item_status` tool names. Bash and unknown tool forms fail
open at the hook and still encounter Sprintctl's authoritative CAS if they
reach the owner.

## Contracts

`work.read.item` now includes the existing opaque `status_revision` beside its
description `edit_revision`. `work.read.item-projection` returns an allowlisted
`work-item-context/v1` object capped at 4,096 UTF-8 bytes. The title is
semantically truncated at a UTF-8 boundary and the projection labels domain
values as untrusted data. It includes no descriptions, refs, events,
credentials, environment, worktree path, or raw logs.

`work.validate.item-status-mutation` is a read-only early-feedback operation.
It reports missing, malformed, matching, or stale expected revisions with the
current bounded projection. It never grants authority and never substitutes
for `set_work_item_status` / `work.lifecycle.arbitrate`, which compare again at
the owner boundary.

The local adapter requires an explicitly bound `SPRINTCTL_CONTEXT_ITEM_ID` and
the existing corroborated served-backend configuration. Its cursor is a
disposable per-repository/item/harness/session/subagent revision file. Cursor
loss can cause reinjection only. Session/subagent projections and changed
deltas fail open; recognized status prechecks fail closed on missing context,
stale revision, mismatched item, or served API outage. The adapter writes no
authority state and is not installed into Claude or Codex settings by this
change.

## Rollout and rollback

This commit and its package entry point are inert until an operator adds a
native hook configuration and binds one item. Rollback is removal of that hook
configuration. Sprintctl CAS remains enabled and requires no schema rollback.
Appservice configuration, credentials, cluster reconciliation, and deployment
are separate operator-owned work and are not part of this pilot.
