# Remote authority commands

The authority-command journal is the opt-in migration path from direct backend
mutations to the outbox model in `adr-outbox-sync-model`. It covers item
transition and completion; sprint activation and close; and capability-receipt
acceptance.

The path defaults to `off`. Existing item, sprint, and receipt commands
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
authority validates the command basis, close boundaries, and receipt artifacts
when a corresponding served command is invoked or an already-recorded request
is retried through served `authority sync`.

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

`capability-receipt.accept` records are the one exception: the served batch
operation does not support them, so `sync` reports them under
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
