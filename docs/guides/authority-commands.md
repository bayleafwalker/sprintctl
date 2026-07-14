# Remote authority commands

The authority-command journal is the opt-in migration path from direct backend
mutations to the outbox model in `adr-outbox-sync-model`. It covers claim
acquire, renew, handoff, and release; item transition and completion; sprint
activation and close; and capability-receipt acceptance.

The path defaults to `off`. Existing claim, item, sprint, and receipt commands
continue to use their current backend implementation. Enabling this path does
not silently intercept those commands; operators invoke the explicit
`sprintctl authority` surface while rollout evidence is gathered.

## Modes

```sh
sprintctl authority status --json
sprintctl authority mode --set shadow
sprintctl authority mode --set enforce
```

| Mode | Durable local request | Remote arbitration | Authority mutation |
|---|---:|---:|---:|
| `off` | no | no | only through the retained commands |
| `shadow` | yes | no | never from the authority request |
| `enforce` | yes | required | only with an accepted remote decision |

`enforce` requires the configured remote PostgreSQL backend. It fails before
appending when the repository is local, so it cannot make a local request look
effective. `shadow` is useful for inspecting command shapes and proof handling,
but a shadow request is pending evidence, not a successful transition.

## Submit commands

Every submission records a strict command envelope in
`.sprintctl/authority-command-outbox.db`. Raw claim proofs are forbidden in the
envelope; only their SHA-256 bindings are durable.

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

# Acquire a claim. A new proof is generated and privately retained when no
# proposed proof is supplied.
sprintctl authority submit \
  --type claim.acquire \
  --aggregate-id 42 \
  --payload '{"agent":"worker-a","claim_type":"execute","exclusive":true,"ttl_seconds":600,"metadata":{}}' \
  --actor worker-a \
  --json
```

The CLI reads the current aggregate revision unless `--basis-revision` is
given. PostgreSQL locks and revalidates current state, dependencies, active
claims, expiry, proof, close boundaries, and receipt artifacts. A stale or
invalid request commits as an immutable request plus `command.rejected`; it
does not mutate the aggregate. Retrying the same event and stream identity
returns the first decision.

Use environment variables for existing proofs so they do not enter shell
history:

```sh
export SPRINTCTL_AUTHORITY_CLAIM_TOKEN='<existing proof>'
sprintctl authority submit \
  --type claim.renew \
  --aggregate-id 150 \
  --payload '{"ttl_seconds":600}' \
  --actor worker-a \
  --json
unset SPRINTCTL_AUTHORITY_CLAIM_TOKEN
```

Coordinator and explicitly pre-minted proofs can similarly use
`SPRINTCTL_AUTHORITY_COORDINATE_CLAIM_TOKEN` and
`SPRINTCTL_AUTHORITY_PROPOSED_CLAIM_TOKEN`.

## Lost responses and proof recovery

Proof-bearing requests retain every transient proof needed for retry in one
event-keyed sidecar under `.sprintctl/authority-credentials/`. The directory is
mode `0700`, each file is mode `0600`, and content is digest-checked before
use. Requests and decisions contain no raw proof. Handoff sidecars retain both
the old proof required for arbitration and the proposed proof, while recovery
returns only the proposed proof.

```sh
sprintctl authority sync --json
sprintctl authority recover-proof --event-id <request-uuid>
sprintctl authority clear-proof --event-id <request-uuid>
```

`sync` retries pending commands only when all required proof bindings can be
resolved. Commands without locally available proof remain pending and cannot
change authority. Accepted acquire and rotating-handoff sidecars remain until
the caller stores the new proof and runs `clear-proof`; sidecars for other
completed or rejected commands are removed.

Treat `recover-proof` output as a secret. Do not paste it into notes, logs,
JSON artifacts, or command payloads.

## Decision evidence and atomicity

The remote transaction admits the command request, applies or rejects the
semantic effect, appends the remote decision, and records its request/decision
binding atomically. An infrastructure failure rolls all of those writes back.
Semantic rejection rolls back only the attempted effect and commits the
request plus rejection decision.

The local decision cache has an independent sparse watermark because command
requests, observations, and decisions share the server ingest sequence. The
cache is evidence and offline context; it never authorizes a transition.

## Rollback

```sh
sprintctl authority mode --set off
```

Rollback stops new authority-journal submissions immediately and leaves the
retained backend commands unchanged. Keep the outbox, projection, and proof
sidecars until every accepted new proof has been recovered and every pending
request has an operator disposition. Turning the mode off does not erase
history or revoke an already accepted claim.

This tract deliberately does not switch normal reads to the cached projection
or remove the retained direct backend. Those cutovers require their own parity
and rollout gates.
