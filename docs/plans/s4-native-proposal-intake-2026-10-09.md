# S4 native proposal producer carrier

This bounded producer slice uses the existing `work.effect.propose-v1` owner.
It does not change that operation's catalog descriptor, admission semantics,
capability, canonical six-field content digest or owner schema.

## Closed capture

`authority proposal-queue --request <file> --run-binding <file>` captures exactly
three envelope fields: `schema_version: native-proposal-request/v1`,
`operation: work.effect.propose-v1`, and `arguments`. The arguments are exactly
`run_id`, positive integer `item_id`, `repository`, `base_commit`, `title`,
`rationale`, `unified_diff`, and `idempotency_key`. The key belongs inside the
arguments; this operation forbids an invocation-envelope key. Text limits and
commit/key patterns match the owner. Content whitespace is preserved.

The separate registered run binding contains exactly `repo_id`, `run_id`,
`principal_id`, `workspace_id`, `client_id`, and `grant_id`. Original request and
binding bytes, their hashes, normalized objects and append-only attempt receipts
share the producer outbox database in dedicated tables. They consume no
observation origin sequence and convey no offline authority. Duplicate normalized
content retains the first authored bytes. Reusing a scoped owner key with changed
content, run or grant binding is refused. Credential-shaped payloads, duplicate
JSON keys and non-finite values are rejected before producer initialization.

## Explicit synchronization

`authority proposal-status` reads pending state without initializing tables or
connecting to authority. `authority proposal-sync` holds its dedicated process
lock and uses one lazy credential snapshot for the entire pass. Each native RPC
uses a fresh asynchronous client. It resolves and compares the full registered
binding, then submits the exact captured argument key and content. A durable
started attempt precedes each RPC. Confirmation requires the same repository,
run, item, proposer principal and content plus a recomputed canonical intent
digest. Exact replay may return a currently accepted, rejected or applied intent
with a later revision; confirmation does not require revision one or proposed
state. Any owner-supplied Release identifies the owner's recorded proposal; it
is not proof of a producer-observed causal Release basis.

Refusal, reply loss, malformed receipts or interrupted local confirmation remain
pending and stop dependent FIFO requests. Original request and confirmed receipt
hashes are checked before status or retry. There is no correction, withdrawal or
silent supersession in this slice. Generic `authority sync` only reports pending
proposal identifiers or an explicit unavailable-status error; it never dispatches
proposal requests through observation batch transport.

## Qualification boundary

Temporary producer tests cover closed capture, whitespace, immutable bytes,
append-only history, receipt damage, lifecycle replay, interrupted attempts,
head-of-line uncertainty, CLI separation and credential replacement. Disposable
PostgreSQL tests exercise actual committed owner effects with lost replies,
replay after lifecycle transitions, changed caller/binding, missing-item FIFO,
local confirmation crash and independent producers sharing one scoped key.

Concurrent causal admission requires a separately reviewed additive owner
operation binding a previously observed full Release revision, Release digest,
native reserve key, trailer-bearing commit and confirmed evidence entry tail.
This carrier does not establish that causal invariant, actual authority-offline
and online equivalence, legacy import readiness, hook cutover or a soak date.
