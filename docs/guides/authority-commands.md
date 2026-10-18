# Remote authority commands

The authority-command journal is the opt-in migration path from direct backend
mutations to the outbox model in `adr-outbox-sync-model`. It covers item
transition and completion; sprint activation and close; and work decisions
(`decision.record`).

Completion is a decision. `item.done`, and `item.transition` to `done`, record
an `accept` decision and bind the item to it in the same transaction, so
outboxes written by older clients still apply. `decision.record` records any
kind (`accept`, `reject`, `withdraw`, `supersede`, `revise`) with its
rationale and evidence digests; `supersede` names the superseding item by
`superseded_by_aggregate_uuid`. A decision binds the item's current Release
unless it names a `release_digest`, which must be a release of that item (see
`docs/protocols/reservation-model.md`). The former capability-receipt commands are
retired and every `capability-receipt*` type is refused.

The path defaults to `off`. Existing item and sprint commands
continue to use their current backend implementation. Enabling this path does
not silently intercept those commands; operators invoke the explicit
`sprintctl authority` surface while rollout evidence is gathered.

## Modes

```sh
sprintctl authority status --json
sprintctl authority mode --set shadow
sprintctl authority mode --set enforce
```

| Mode | Generic `authority submit` | Authority mutation |
|---|---:|---:|
| `off` | no | only through the normal local or served commands |
| `shadow` | appends a local pending record | never from that record |
| `enforce` | retired; no direct PostgreSQL submission | through the corresponding served work command |

`authority submit` while the rollout mode is `enforce` no longer opens a direct
PostgreSQL arbitration path. It fails before opening a normal backend store. Use the
corresponding served lifecycle or work command; `authority sync` in a
served environment retries an already-recorded request. `shadow` remains useful
for inspecting command shapes, but a shadow request is pending evidence, not a
successful transition.

## Shadow submit commands

Every **shadow** submission records a strict command envelope in
`.sprintctl/authority-command-outbox.db`. Secret material is forbidden in the
envelope: `contracts.py` rejects it by field name, and no command payload
contract accepts a proof or a proof reference.

```sh
# Item 42: pending -> active
sprintctl authority submit \
  --type item.transition \
  --aggregate-id 42 \
  --payload '{"to_status":"active"}' \
  --actor operator \
  --json

# Sprint 7: active -> closed, including its close-boundary event
sprintctl authority submit \
  --type sprint.close \
  --aggregate-id 7 \
  --actor operator \
  --json
```

The CLI reads the current local aggregate revision unless `--basis-revision`
is given. Shadow submissions do not mutate shared authority. The served
authority validates the command basis, transitions, and decisions
when a corresponding served command is invoked or an already-recorded request
is retried through served `authority sync`.

## Authority preflight

With an existing validated served profile, inspect the producer before work:

```sh
sprintctl authority doctor --served --check-permissions --check-sequence --json
```

The diagnostic reports each selected check as `pass`, `fail` or `unknown`.
It checks terminal-directory ownership and exact `0700` mode, a single local
stream with contiguous sequences, current-stream quarantine/rollover evidence,
and the served cursor plus immutable committed record content. Missing cursors,
missing history, unavailable reads, concurrent changes and inspection limits
remain unknown; a matching next sequence alone cannot establish alignment.
The existing producer and ingestion fingerprint domains remain distinct: the
comparison reuses the owner's canonical portable-record validator.

The default path reads only: it never initializes or migrates an outbox,
allocates a sequence, replays a command or writes a recovery receipt. SQLite
may use WAL/SHM reader bookkeeping while its data transaction is read-only.
Inspection covers at most 10,000 local records/receipts and 10,000 served rows,
with 1 MiB per record/receipt and 64 MiB cumulative text/record/receipt budgets.
Incomplete history is unknown, never inferred empty. No diagnostic changes
directory modes. The strict mode check can flag inherited `02700` even where
the retained runtime explicitly tolerates that otherwise-private directory.
Retired claim-recovery and authority-credential directories are not recreated.

An authenticated read does not prove write access. Without an explicit probe,
`write_probe` remains unknown and the command exits 1. To record ordinary
activity on an existing active reservation in the current session, explicitly
select its ID:

```sh
sprintctl authority doctor --served --write-probe-reservation-id "$MY_RESERVATION_ID" --json
```

The probe runs only after clean selected checks and compares the authenticated
actor and current session with the reservation before and after touching it.
These are advisory metadata checks: reassignment can race the touch, and a
lost response leaves its activity outcome unknown. The command never retries
the write automatically or creates test work. A pass records only the specific
operations actually exercised; it does not prove authorization for another
operation or make the reservation a permission token.

Exit 0 requires every selected check, including the explicit probe, to pass;
failures and unknowns exit 1 after producing the report. This is an operator
aid, not a migration/cutover gate or a replacement for backup and owner
authorization checks. It introduces no served operation, schema or grant.

## Lost responses and retry

The producer log is immutable, so a lost response is always safe to retry with
the *same* durable request: re-running `authority submit` with the original
`--event-id` reuses the recorded envelope instead of minting a new one, and the
served side keys idempotency off that `event_id`.

```sh
sprintctl authority sync --json
```

`sync` sends every outbox record that has no terminal decision receipt, in
order. A conclusively accepted or rejected command gets a local receipt under
`.sprintctl/authority-terminal-decisions/` (mode `0700`, each file `0600`,
validated before use) so later passes skip it; an unknown transport outcome
writes no receipt and stays replayable. Local direct-PostgreSQL `sync` is
retired.

Authority commands the served batch operation does not support
(`decision.record`, which served clients record directly through
`work.decision.record` / `sprintctl item decide`, or a retired type left in an
old outbox) are the one exception: `sync` reports them under
`unsupported_command_event_ids` rather than failing the chunk that contains
them.

Transient proof sidecars under `.sprintctl/authority-credentials/` are retired
along with claim arbitration. No command payload contract accepts proof
material, so no record can stall a sync pass waiting for one.

## Served-authoritative recovery

When a served stream has historical local records that cannot be replayed,
audit it before making any local recovery change:

```sh
sprintctl authority reconcile --json
sprintctl authority reconcile --apply --json
```

`reconcile` reads the served record and decision ledgers and treats them as
authoritative. `--apply` is refused on a semantic conflict. It writes local
receipts only: matching served decisions settle their local producer records;
an older local sequence that is absent below the served stream high-water is
recorded as `absent-from-served-ledger` and is never replayed. It does not
rewrite a local outbox, advance or rewind a served cursor, or invent a served
decision. Preserve the JSON audit output with the incident record.

The producer outbox fingerprint and served ingestion fingerprint are distinct
integrity domains. Compare canonical record content through `reconcile`, not
their separately stored `record_sha256` columns.

## Served decision evidence and atomicity

The served authority admits the command request, applies or rejects the
semantic effect, appends its decision, and records its request/decision binding
atomically. An infrastructure failure rolls all of those writes back. Semantic
rejection rolls back only the attempted effect and commits the request plus
rejection decision.

The local decision cache has an independent sparse watermark because command
requests, observations, and decisions share the server ingest sequence. The
cache is evidence and offline context; it never authorizes a transition.

## Rollback

```sh
sprintctl authority mode --set off
```

Rollback stops new authority-journal submissions immediately and leaves the
retained backend commands unchanged. Keep the outbox, the projection, and the
terminal-decision receipts until every pending request has an operator
disposition. Turning the mode off does not erase history or revoke an already
accepted decision.

This tract deliberately does not switch normal reads to the cached projection
or remove the retained direct backend. Those cutovers require their own parity
and rollout gates.
