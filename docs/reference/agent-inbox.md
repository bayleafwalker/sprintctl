# Agent directory and durable inbox (store side)

Vuoro owns the contract: `docs/architecture/agent-directory-and-inbox.md`
(contract v1, with the vuoro#219 `launch-ambiguous` amendment) and the
`agent-{declaration,message,message-receipt,directory-entry}-v1` schemas.
Copies of the schemas live in `tests/fixtures/agent-inbox-v1/`, and the tests
validate every result against them. Sprintctl is the store owner. PostgreSQL
schema 25 introduced these ten operations; serving them follows the current
[remote-schema compatibility gates](postgres-schema-compatibility.md).
Standalone SQLite refuses each one with `unsupported-backend`.

| Operation | Authority | Arguments | Result |
|---|---|---|---|
| `work.agent.register-v1` | `work:evidence` | `instance_id`, `declaration`, `idempotency_key` | `instance_id`, `declaration_revision`, `replayed` |
| `work.agent.observe-v1` | `work:evidence` | `instance_id`, `fact`, `channel`?, `run_id`?, `valid_for_seconds`, `idempotency_key` | `instance_id`, `fact`, `observed_at`, `valid_until`, `source`, `replayed` |
| `work.agent.retire-v1` | `work:evidence` | `instance_id`, `reason`, `idempotency_key` | `instance_id`, `retired_at`, `replayed` |
| `work.agent.list-v1` | `work:read` | `include_retired`? (default true) | `entries` (directory entries) |
| `work.agent.describe-v1` | `work:read` | `principal_id` | one directory entry |
| `work.message.send-v1` | `work:evidence` | `message` | the stamps and `replayed` |
| `work.message.read-v1` | `work:read` | `after_seq`, `limit` (1 to 100), `instance_id`? | `messages`, `next_after_seq` |
| `work.message.status-v1` | `work:read` | `message_id` | `message`, `receipts` |
| `work.message.receipt-v1` | `work:evidence` | `message_id`, `instance_id`, `kind`, and the fields of that kind | the receipt and `replayed` |
| `work.message.cancel-v1` | `work:evidence` | `message_id`, `reason` | the `cancel-requested` receipt and `replayed` |

The store takes `principal_id` and `workspace_id` from the authenticated
identity and `repo_id` from the call's scope. It never reads them from
arguments. A record outside the caller's `(workspace, repo)` is `not-found`.

## Receipt meaning and the work boundary

The inbox records message handling. Its state is separate from native process
state, durable work leases, work acceptance and external effects:

| Record | Meaning | Does not establish |
| --- | --- | --- |
| Successful `send` | The scoped inbox has retained the message. | Receiver observation or native execution. |
| `observed` / `acknowledged` | A recipient instance reports receipt/acknowledgement. | Capacity, work ownership or effect permission. |
| `accepted` | One instance of the recipient principal accepts handling. | Reserved runtime capacity, an acquired work lease or a terminal work Decision. |
| `run-linked` | The acceptor links an eligible run. | A running native process or successful execution. |
| `launch-ambiguous` | The acceptor records uncertainty about launch. | Failure, non-invocation or permission to launch again. |
| `result-linked` | A permitted instance records an outcome and evidence references. | Independent verification of that outcome or settlement of the work item. |
| `cancel-requested` | The sender asks for cancellation. | Cancellation acknowledged, the process stopped or its effects reconciled. |

The current `accepted`/`refused` decision is single-winner for the message,
not a per-instance renewable offer. It has no automatic expiry/reoffer path.
Directory liveness observations do not transfer acceptance, and another
principal cannot acquire a message by becoming more available. A transiently
busy instance must not write the message-wide `refused` receipt unless the
recipient's policy means to decline that message.

Capacity and native permission checks belong to the consuming adapter and
execution owner. These checks can precede acceptance, but the inbox and an
external runtime are not one transaction: admission must still be confirmed
at launch. The [portable workflow plan](../plans/2026-10-24-portable-agent-workflows.md#implementation-refinement-handoff-evidence-and-admission-2026-10-30)
records the implementation and qualification requirements. It does not change
the current receipt schema or add automatic retry to the store.

An evidence reference preserves a link to an owner record or artifact; it
does not import that artifact's bytes, authenticate its source, grant access
or promote its claims into facts. The existing
[effect-attempt boundary](effect-attempts.md) and
[durable work lease](../protocols/reservation-model.md#the-durable-work-lease-is-a-separate-mechanism)
continue to govern invocation and settlement independently of inbox outcomes.

## Choices the contract leaves open

- Idempotency: register, observe and retire claim the existing idempotency
  ledger with their `idempotency_key`, using the same key rules as
  `work.session-note.write-v1`. Send claims the ledger with its `message_id`.
  A same-sender retry with a different envelope gets `message-id-conflict`,
  not `idempotency-conflict`.
- `inbox_seq` is allocated from the `agent_inbox_counter` row with
  `UPDATE … RETURNING` inside the send transaction. Sends to one repository
  therefore commit in sequence order, and a send that rolls back leaves no gap.
- Precedence for a receipt: visibility first (`not-found` for anyone but the
  recipient principal), then instance ownership (`not-found` for an unknown
  instance, `instance-not-owned` for another principal's instance). Next comes
  an exact replay, then `instance-retired`, then the conflict and prerequisite
  codes of the kind. A targeted message is `not-found` to any other instance
  of the principal, except for `result-linked` `abandoned`.
- `completed`, `failed` and `canceled` without `run-linked` are refused with
  `not-accepted`. So is a `result-linked` written by an instance that did not
  accept, unless the acceptor is retired and the outcome is `abandoned`.
- Re-registering or re-retiring a retired instance is `instance-retired`.
- `reachable` lists the channels of the latest declaration, followed by any
  other channel that has been observed.
- Reading with an `instance_id` the caller has not registered in scope is
  `not-found`.

## Test clock

The owner reads time from `clock_timestamp()`. A session setting alone never
changes that, because anyone who shapes a connection can set one (libpq
options, `PGOPTIONS`, `ALTER ROLE/DATABASE ... SET`). The setting
`sprintctl.inbox_test_now` replaces the clock only when all three hold:

- test support has switched on the process-local flag
  `agent_inbox_pg._TEST_CLOCK_ENABLED`;
- the database comment is `sprintctl:disposable-integration-test`;
- the value parses as a timestamp with a time zone.

The test-support functions are `sprintctl.pg_testing.freeze_inbox_clock(store, at=None)`,
`shift_inbox_clock(store, seconds)` and `release_inbox_clock(store)`. The
first two switch the flag on, and the first call to `shift_inbox_clock`
freezes the clock at the current second. Commit after calling any of them.

## CLI

The `sprintctl agent register|observe|retire|list|describe`,
`sprintctl message send|read|status|receipt|cancel`, `sprintctl run context`
and `sprintctl run note` commands each send one operation through the served
client and print its JSON. A declaration, an envelope or a receipt's own
fields are read from `--json FILE` (`-` for stdin). These commands need
`SPRINTCTL_BACKEND=served`.
