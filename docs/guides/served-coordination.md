# Served coordination across repositories

This inventory covers the current CLI and existing served owner operations.
It delivers the note-output and invocation subset of sprintctl#2097 under
child sprintctl#2761. The remaining project workflow interfaces stay open
under #2075 and #2215; this inventory does not qualify those interfaces.
The original appservice#2080 pre-cutover condition is historical, not a new
deployment gate.

## Select the repository explicitly

Use the target repository's real checkout with its validated served marker
and profile, for example:

```sh
cd /projects/dev/appservice
direnv exec /projects/dev/appservice sprintctl item show --id appservice#42 --json
```

For a genuinely markerless directory, the supported explicit invocation is:

```sh
cd /path/to/markerless-directory
export SPRINTCTL_BACKEND=served
export SPRINTCTL_VUORO_PROFILE=/path/to/authorized-vuoro-profile.json
sprintctl --repo-id appservice --allow-markerless-nonlocal \
  item show --id appservice#42 --json
```

The two global options precede the command. They select repository scope for
one invocation; the authenticated credential still determines access and the
recorded actor. A scoped reference selects the same repository as `--repo-id`
and conflicts are refused. Neither the opt-in flag nor a scoped reference can
override a different repository marker or a conflicting inferred git identity.
Do not edit a marker or guess a repository from a numeric ID. Two item
references in one command must select the same repository; they do not create
a cross-repository edge.

## Existing operations

Run the following command suffixes inside the selected target checkout, or
after the explicit markerless prefix above. Numeric IDs are scoped by that
selection; replace them with IDs returned by the corresponding owner reads.
The examples describe current source; check an older installed binary's
`--help` before relying on newer options such as note JSON or scoped reservation
IDs.

| Coordinator task | CLI suffix | Existing owner operation |
|---|---|---|
| Read work and its events/refs/dependencies | `item show --id appservice#42 --json` | `work.read.item` (plus decision read for terminal items) |
| List work in a sprint | `item list --sprint-id 17 --json` | `work.read.items` |
| Read sprint details | `sprint show --id appservice#17 --detail --json` | `work.read.sprint-detail` |
| Read current context | `usage --context --sprint-id 17 --json` | `work.read.context` |
| Read graph suggestions | `next-work --sprint-id 17 --json` | `work.read.next-work` |
| Explain repository suggestions | `next-work --sprint-id 17 --explain --json` | `work.read.next-work-explain` |
| Create work | `item add --sprint-id appservice#17 --track maintenance --title 'Exact task' --description 'Bounded scope' --json` | `work.item.create` |
| Record a note | `item note --id appservice#42 --type update --summary 'Observed result' --json` | `work.item.note` |
| Read item events | `event list --sprint-id appservice#17 --item-id appservice#42 --json` | `work.read.events` |
| Append ordinary event evidence | `event add --sprint-id appservice#17 --item-id appservice#42 --type update --payload '{"summary":"Observed result"}' --json` | `work.event.add` |
| Attach governing/evidence ref | `item ref add --id appservice#42 --type doc --url docs/runbook.md --label runbook` | `work.item.ref.add` |
| Read/remove refs | `item ref list --id appservice#42 --json`; `item ref remove --id appservice#42 --ref-id 3` | `work.read.item`; `work.item.ref.remove` |
| Read/add repository dependencies | `item dep list --id appservice#42 --json`; `item dep add --id appservice#42 --blocks-item-id appservice#43` | `work.read.item`; `work.item.dep.add` |
| Remove repository dependency | `item dep remove --id appservice#42 --dep-id 3` | `work.item.dep.remove` |
| Read/change ordinary status | `item status --id appservice#42 --status active --json` | `work.lifecycle.arbitrate` after owner basis/identity reads |
| Record terminal outcome | `item decide --id appservice#42 --kind accept --rationale 'Verified result' --evidence <sha256> --json` | `work.decision.record` |
| Coordinate work visibly | `reservation reserve --item-id appservice#42 --role execution --session-id <session> --json` | `work.reservation.reserve` |
| Read reservations | `reservation list --all --json`; `reservation show --id 8 --json` | `work.read.reservations`; `work.read.reservation` |
| Touch/reassign/release reservation | `reservation touch --id 8 --session-id <session> --json`; `reservation reassign --id 8 --session-id <next-session> --json`; `reservation release --id 8 --json` | `work.reservation.touch`; `work.reservation.reassign`; `work.reservation.release` |
| Inspect maintenance findings | `maintain check --sprint-id 17 --json` | `work.maintain.check` |

`reservation list --all` includes released history; omit `--all` for active
reservations only. For an item-specific list use
`reservation list --item-id appservice#42 --json`.
Status writes use the served immutable basis; do not supply the direct-local
`--expected-revision` option. Server actor attribution is authoritative even
when an older command accepts `--actor`. Graph suggestions do not establish
dispatch admissibility; see [project scope](project-scope.md).

## Note JSON and write outcomes

`item note --json` prints only the existing successful result:

```json
{"event_id": 123, "item_id": 42, "note_type": "update", "summary": "Observed result"}
```

Local mode uses its single created event ID for the same shape. Without
`--json`, the existing text and served context line are preserved. JSON is an
output option, not an idempotency key: the CLI sends one note write and does
not retry on response loss. A lost response can follow a committed event;
inspect the owner item/events before deciding whether another write is needed.
Rejections exit unsuccessfully with the existing diagnostic, not a fabricated
success JSON object. No PostgreSQL fallback is available in served mode.

## Distinct coordination and execution mechanisms

Advisory reservations replace the retired coordinator claim verbs. They have
no token, lease or exclusive ownership guarantee. `claim create` and
`claim list-sprint` are not required replacement APIs and are not revived.
Enumerate reservations directly for each explicitly selected repository;
neither next-work nor a reservation list establishes migration quiescence.

The existing protected owner work lease is separate. Its operations are
`work.lease.acquire-v1`, `work.lease.heartbeat-v1`,
`work.lease.report-outcome-v1` and `work.lease.read-v1`, described in the
[work adapter reference](../reference/vuoro-work-adapter.md). They are not a
`claim` CLI revival or interchangeable with `reservation reserve`, and this
inventory does not mint or exercise a live lease or maintenance capability.

## Open project interfaces

The current served project list/context/next-work reads use the server's
canonical project binding and authorize its members. They cannot be redirected
by a caller-provided project file. Repository marker mismatch checks still
apply to the invoking CLI.

- `next-work --project --explain` is explicitly unavailable in the served CLI,
  even though `work.project.next-work-explain` exists in the catalog. The
  current supported explain invocation is per repository, as above. The
  project aggregate CLI delivery remains #2075, including its #2095 readiness
  prerequisite.
- Existing `item dep` commands are repository-scoped. They do not provide
  cross-repository dependency semantics. That owner interface remains #2215
  behind the qualified foundation gate.
- A complete current project coordinator workflow still needs those owner
  interfaces and workflow qualification. Parent #2097 stays open; accepting
  the note JSON/inventory child does not accept the remaining workflow.
