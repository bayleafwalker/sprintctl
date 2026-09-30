# Vuoro work adapter and application core

The sprintctl work adapter exposes sprintctl-owned state semantics through the
Vuoro protocol-v1 catalog. The reusable Vuoro shell supplies transport,
identity, authority checks, schema validation and envelopes; sprintctl keeps
work reads, reservation arbitration, lifecycle transitions, evidence ingestion,
batch ordering and project behavior in its own application package.

`sprintctl.application` is Click-independent. `sprintctl.vuoro_adapter` has no
import-time dependency on `vuoro-service`; service composition imports the
pinned domain release and calls `register_work_catalog`. Standalone and marked
recovery SQLite authorities continue using the legacy CLI. Normal shared
service composition uses `WorkApplication.postgres()` only after the service
compatibility gate has passed; importing or registering the adapter performs
no migration or DDL.

## Catalog v1

| Surface | Operations | Idempotency |
| --- | --- | --- |
| Reads | `work.read.sprints`, `work.read.item`, `work.read.context`, `work.read.context-candidates`, `work.read.next-work`, `work.read.records`, `work.read.decisions` | key forbidden |
| Item edit | `work.item.edit` | key forbidden; required `expected_revision` compare-and-swap |
| Volatile item context | `work.read.item-projection`, `work.validate.item-status-mutation` | read-only, key forbidden; validation is advisory and owner CAS remains final |
| Reservation start | `work.claim.start` | key forbidden; one-shot create plus activation flow — **retired in v2** |
| Durable reservations | `work.claim.arbitrate` | key equals immutable command `event_id` — **retired in v2** |
| Lifecycle | `work.lifecycle.arbitrate` | key equals immutable command `event_id` |
| Decisions | `work.decision.record` | key required; a replay with the same key returns the first decision |
| Decision and release reads | `work.read.item-decisions`, `work.read.release`, `work.read.unbound` | key forbidden |
| Evidence | `work.evidence.ingest` | key equals canonical record-batch digest |
| Batching | `work.batch.apply` | key equals canonical ordered-project-batch digest |
| Project | `work.project.context`, `work.project.sprints`, `work.project.items`, `work.project.next-work`, `work.project.batch` | aggregates require a canonical binding and authorization for every member; writes use canonical ordered-project-batch digest |
| Maintenance capability | `work.read.maintenance-capability`, `work.maintenance.prepare`, `work.maintenance.transition` | read forbids a key; mutations require the invocation key to equal the immutable request ID |
| Maintenance recovery evidence | `work.maintenance.recovery-record` | key equals the immutable recovery record ID; the result always declares `authority=none` |
| Cutover evidence | `work.pilot.cutover-evidence` | key forbidden |
| Runs (0.8.0, schema 17) | `work.run.register-v1`, `work.run.resolve-v1` | register requires an `idempotency_key` argument (write-tool ledger); resolve forbids one |
| Run continuation (schema 20, agentops#2525) | `work.run.predecessor-context-v1`; optional `predecessor_run_id` on `work.run.register-v1` | read, `work:read`, forbids a key; its presence in the catalog is the capability signal for both |
| Run evidence and notes (0.8.0, schema 17) | `work.evidence.tail-v1`, `work.evidence.append-v1`, `work.session-note.write-v1` | tail forbids a key; append and note writes require an `idempotency_key` argument (write-tool ledger) |
| Effect intents (schema 19) | `work.effect.propose-v1`, `work.effect.get-v1`, `work.effect.list-proposed-v1`, `work.effect.accept-v1`, `work.effect.reject-v1`, `work.effect.mark-applied-v1` | propose requires an `idempotency_key` argument (write-tool ledger); every other operation forbids one, and a transition is a compare-and-set on `revision` and `canonical_intent_digest` |
| Work leases (0.9.0, schema 18; report-outcome from 0.10.0) | `work.lease.acquire-v1`, `work.lease.heartbeat-v1`, `work.lease.report-outcome-v1` (and its deprecated alias `work.lease.complete-v1`), `work.lease.read-v1` | acquire and report-outcome require an `idempotency_key` argument (write-tool ledger); heartbeat and read forbid one |

Every operation declares JSON Schema 2020-12 input and result contracts,
authority, execution semantics, idempotency behavior and required client
schema features. Record schemas use local `$defs` references only.
`work.pilot.cutover-evidence` is intentionally catalog-described rather than
hard-coded into the client, so an already-installed protocol-v1 client can
refresh discovery and invoke it.

`work.read.item` includes an `edit_revision` derived from the item identity,
the current description digest, and the append-only count of prior
`item-edited` events. `work.item.edit` requires that revision and locks the
item while comparing it. A successful edit updates the description and
appends one `item-edited` event in the same transaction; the event records the
old/new revisions and descriptions. Existing events and item identity are
never rewritten. A stale revision is rejected as `item-edit-conflict`, and an
unchanged description is rejected without creating another revision.

`work.read.item` also exposes the opaque item `status_revision` already used by
direct and served lifecycle CAS. `work.read.item-projection` emits a bounded,
field-allowlisted status projection for native runtime context. The companion
`work.validate.item-status-mutation` operation only gives early feedback; it
does not reserve, mutate, or authorize an item, and the lifecycle owner repeats
the comparison atomically. See
[`volatile-context-native-hook-pilot.md`](../plans/volatile-context-native-hook-pilot.md).

`work.read.context` is the server-side aggregate for `usage --context`. It
returns the ContextContract v1 itself (rather than adding an envelope field),
and PostgreSQL evaluates all of its sprint, reservation, item, dependency, stale
work, and decision reads in one repeatable-read, read-only transaction. A
client must not recreate this operation by stitching together raw read calls.

`work.read.context-candidates` is the server-side Tier-1 dispatch packet for
ActionQ and other bounded workers. It uses the established deterministic
ranking function over one repository's ready items and refs. An explicit
pending target is the only reservation-eligible result; invocation never
reserves or starts work.

`work.decision.record` records a work decision (`accept`, `reject`,
`withdraw`, `supersede`, `revise`) with its rationale and evidence digests, as
the authenticated identity; the contract has no actor argument. It returns the
decision row with the item's resulting `status`, `resolution` and
`terminal_decision_id`, and `replayed`. The decision is written with one
`item-decided` event carrying the request idempotency key (the event type is
reserved for this writer), so a retry under the same key returns the first
decision and the same key with a different request is refused
(`idempotency-conflict`, 409). Refusals: `release-mismatch` (a digest that is
not a release of the item), `invalid-transition` (for example accepting a
non-active item), `item-terminal`, `legacy-done-item` (all 409),
`decision-rejected` (422) and `item-not-found` (404).
A legacy item that was done before decisions existed and has no decision
takes one *re-mark* through the same operation: a terminal kind with a
non-empty rationale and at least one evidence digest. The item stays done and
keeps its `updated_at`; the `item-decided` event carries `legacy_remark:
true`. A re-mark without a rationale or evidence, or a `revise`, is refused as
`legacy-done-item` (409); once bound, the decision is immutable, so a second
one is `item-terminal`. Schema 16 (SQLite 25) enforces the same in its
trigger.
`work.read.item-decisions` lists an item's decisions oldest first.
`work.read.unbound` lists items not bound to a decision in three categories
-- `legacy_done`, `decided_unreleased`, `released_undecided` -- each with its
total `count` and up to `limit` (default 100, at most 1000) `items`,
optionally for one `sprint_id` or one `category`, and `resolutions`: done
items counted as `accepted`, `rejected`, `withdrawn`, `superseded`,
`decided_done`, `legacy_done`, `legacy_remarked` and `done`. A re-marked
legacy item is in no category; it counts under its resolution and in
`legacy_remarked`.
`work.event.add` and `work.item.note` refuse decision-like event types with
`decision-like-event-type` (422), so a note cannot pose as a decision.
`work.read.release` returns a release and its `release_commit` rows, by
`release_digest` or for an `item_id`'s current release. `work.read.decisions`
is the authority-decision journal read and is unrelated.

## Runs, evidence and session notes

Added in sprintctl 0.8.0 with remote schema 17 (agentops#2466, E2). These
operations back the Vuoro MCP edge's record bucket (`work:evidence`) and are
served only by the PostgreSQL authority; the local SQLite backend has no run,
evidence or ledger storage.

- `work.run.register-v1` mints a `run_<ULID>` bound to the caller: principal,
  workspace and repository, plus the OAuth `client_id` and `grant_id` when the
  identity asserts them (null otherwise). None of these come from arguments.
- `work.run.resolve-v1` echoes that whole binding. A run bound to any other
  principal, workspace, client or grant, including another grant of the same
  principal, is `run-not-found` (404), with the same code and message as an
  unknown id. The evidence and note operations resolve the run the same way
  first.
- `work.evidence.append-v1` extends the run's chain under a per-run lock.
  `chain_seq` must be one past the stored tail. `chain_prev_digest` must be
  the tail's `entry_digest` as `vuoro_evidence.core.chain` defines it, or null
  for the first item. An `item_id` may not already be stored under another
  key. Any violation, including a lost race for the tail, is
  `evidence-chain-conflict` (409).
- Write-tool idempotency (E2/E3 shared contract section 5): the ledger is
  keyed by (workspace, principal, tool, key). One transaction claims the
  ledger row, performs the write and records its result. The same key with
  the same arguments replays the stored result. The same key with different
  arguments is `idempotency-conflict` (409), and the loser never performs
  its write. For `register_run` the grant binding counts as part of the
  arguments. The run, evidence item or note also stores the request digest,
  so storage enforces the same rule on its own. A failed write commits
  nothing, so the key can be retried.
- Continuation (schema 20, agentops#2525; vuoro E2/E3 shared contract
  section 4). `work.run.register-v1` takes an optional `predecessor_run_id`:
  a run of this repository in the caller's workspace, whose principal,
  client and grant may differ. Callers omit it for "no predecessor"; the
  schema admits only a string. Naming one needs `work:read` as well as
  `work:evidence` (`authority-required`, 403). Anything else (unknown,
  malformed or another workspace's run) is `predecessor-not-eligible` (422),
  one code and one message for all, and writes nothing. The argument counts
  toward the request digest only when present, so the same key naming
  another predecessor (or none) is `idempotency-conflict`, and a request
  without one keeps its earlier digest. The run result echoes
  `predecessor_run_id`.
- `work.run.predecessor-context-v1` (`work:read`) takes the caller's own
  `run_id`, resolved to its exact binding (`run-not-found` otherwise), and
  returns `predecessor_run_id` with one page of that predecessor's session
  notes (`note_id` order) and evidence items (`chain_seq` order). Each list
  is paged by its own cursor: at most `limit` (default 100, maximum 500)
  entries after `after_note_id` / `after_chain_seq`, with
  `next_after_note_id` / `next_after_chain_seq` set to the next cursor when
  more remain and null when the list is exhausted. A run without a
  predecessor returns null and empty lists. Notes of one run are written
  one at a time (a per-run lock, like the evidence chain's), so `note_id`
  order is commit order and a cursor never skips a note even while the
  predecessor is still writing. One hop only. The predecessor's
  run never resolves to the successor, so continuation transfers context,
  not authority.
- **Confidentiality boundary.** Through continuation, any principal in the
  workspace holding `work:read` and `work:evidence` can register a run that
  names another principal's run (run ids are visible to it, for example
  through `work.lease.read-v1`) and read all of that run's session notes
  and evidence. There is no opt-out. Session notes and evidence are
  therefore readable workspace-wide within the repository and must not
  hold secrets or anything the workspace's other members may not see.
- A run that another run continues cannot be deleted (`ON DELETE
  RESTRICT`); deleting the successor removes only its link.

## Work leases

Added in sprintctl 0.9.0 with remote schema 18 (agentops#2520, E2b). These
operations back the Vuoro MCP edge's coordinate bucket (`work:claim`:
`claim_work`, `heartbeat`, and `complete_work`, which becomes
`report_outcome`) and are served only by the
PostgreSQL authority. `work.lease.read-v1` needs `work:read`. The retired
`work.claim.*` names stay retired; leases are a new surface, not their
return.

A lease is exclusive: at most one `active` lease per item. It is held by the
caller's run (`run_id`, resolved first as the record operations resolve it)
and that run's whole binding: principal, workspace, OAuth client and grant.
Advisory reservations are separate and stay advisory; a lease neither
refuses nor is refused by them.

Catalog change in 0.10.0 (agentops#2539): the lease
results' `verification` object changes `profile` from an enum to a pattern
(a combined bar is named with `+`) and makes `requirements` required. The
operation names and `-v1` versions stay, and a lenient consumer is
unaffected, but the catalog metadata digest changes, so a pinned consumer
(vuoro `scripts/validate_released_work_adapter.py`
`_EXPECTED_WORK_METADATA_SHA256` and
`scripts/validate_released_catalog_composition.py` `EXPECTED_REVISION`)
must re-pin when it adopts that release.

The same release aligns the lease with the operator's lease contract
(agentops#2540; semantics 1-5 in agentops
`docs/plans/2026-09-27-backlog-ideation.md`, R4). No client consumed the
lease operations yet, so this changes the `-v1` contracts in place rather
than versioning them: `ttl_seconds` leaves the acquire input, leases gain
`generation` and `heartbeat_interval_seconds`, `work.lease.report-outcome-v1`
is added with `complete-v1` as its deprecated alias, report results gain
`settlement_effect`, `lease-superseded` becomes `claim-superseded`, and
the `lease.taken-over` event becomes `work.claim.taken-over`. The catalog
digest changes with them.

Contract names and the wire codes: the contract's `CLAIM_SUPERSEDED` is
published as `claim-superseded`, in the kebab-case every other code uses
(as the contract's `IDEMPOTENCY_KEY_REUSED` is `idempotency-conflict`).
The same holds for the other contract names:

| Contract | Published |
|---|---|
| `CLAIM_SUPERSEDED` | code `claim-superseded` (409), `details: {claim_id, current_generation, reported_generation}` |
| event `work.claim.taken_over`, `reason = stale_lease` | event `work.claim.taken-over`, `reason: stale-lease` |
| `outcome.reported` with `disposition = stale`, `settlement_effect = none` | event `lease.outcome-reported`; the report is `disposition: rejected` with a `reason_code` (`claim-superseded`, `lease-expired`, ...), and nothing settles |
| `report_outcome` | `work.lease.report-outcome-v1`, result `settlement_effect` |
| `ttl` 10 min, `heartbeat_interval` 2 min | `ttl_seconds` 600, `heartbeat_interval_seconds` derived as TTL/5 (not configured separately) |

A reactivated lease keeps the `ttl_seconds` it was acquired with.
A 0.9.0 `complete_work` ledger key retried under `report_outcome` is
`idempotency-conflict`, not a replay: the ledger digest includes the tool
name. No client used it.
A **claim** is a lease: `claim_id` is the `lease_id`. Its **generation**
is the lease's position among the item's leases in acquisition order
(1 for the first); it is derived, not stored, so it needs no migration.

This authority evaluates a lease whenever someone calls, against its own
clock. Nothing expires, sweeps, schedules or retries in the background
(TS-1). A lease is stale once `heartbeat_at + ttl_seconds` has passed.
The TTL is authority configuration, never the caller's: 600 seconds
unless the runtime sets `SPRINTCTL_LEASE_TTL_SECONDS` (clamped to 30-3600;
a malformed value falls back to 600). A tenant runtime serves one
workspace, so that is also the per-workspace setting. Every lease
advertises `heartbeat_interval_seconds`, a fifth of its TTL (120 s by
default). A claim naming `ttl_seconds` is `invalid-arguments` (422).

- `work.lease.acquire-v1` claims an item: missing work is `work-not-found`
  (404); settled work is `work-settled`; a `blocked` item or one waiting on
  an unsettled blocker is `work-blocked`; an item a holder parked (see
  `work.lease.report-outcome-v1`) is `work-parked`; any claim while a maintenance
  capability is active is `maintenance-active`; an item with an outcome
  report awaiting its verifier's decision is `work-awaiting-verification`,
  whoever asks and however fresh or stale the lease (its holder
  re-presenting its own claim included, except that a still-fresh lease
  just has its heartbeat refreshed); an item whose current lease is still fresh is
  `lease-held`; an item whose stored bar needs a verifier this authority
  cannot check yet is `verification-unsupported` (all 409). They are
  checked in that order, so a claim against a fresh lease answers
  `work-awaiting-verification` if a report waits and `lease-held` even if
  the stored bar is unsupported. Another binding's stale lease is taken
  over on demand, with
  no operator reassignment: in one transaction it becomes `superseded`,
  both leases record it (`superseded_by`, `takeover_of`), the new lease is
  the next generation, and a `work.claim.taken-over` event names
  `previous_claim_id`, `previous_principal`, `previous_generation`,
  `previous_last_heartbeat`, `new_claim_id`, `new_principal` and
  `reason: stale-lease`. The same binding claiming under a new key
  replaces its own stale lease: the old one is `released`
  (`replaced-by-holder`), not superseded, and no takeover is recorded. A
  pending item becomes active. The same key with the same arguments
  re-presents the claim and is evaluated again, not replayed; this is how
  a restarted worker resumes (`resumed: true`), and idempotency restores
  the claim's identity, never superseded authority (INV-L2):
  - a fresh lease is the same lease with its heartbeat refreshed;
  - the holder's own stale lease, if nobody took it over, is reactivated
    in place: the same lease id and generation, heartbeat refreshed, and a
    `lease.reactivated` event. It is refused as a fresh claim would be
    (`maintenance-active`, `work-blocked`, `work-awaiting-verification`);
  - a lease taken over is `claim-superseded`, however stale the new
    holder's lease is by then;
  - a settled or released lease is returned as it is.
- `work.lease.heartbeat-v1` refreshes the caller's lease. An unknown lease,
  or anyone else's, is `lease-not-found` (404, one code for both). A
  superseded lease is `claim-superseded`, an ended one `lease-ended`, an
  expired one `lease-expired` even if nobody took it over, and a lease on
  an item moved off `active` `work-not-active` (409). A holder whose lease
  went stale re-presents its claim to reactivate it instead. The heartbeat
  takes the repo claims lock first (then the item row, then the lease row),
  the lock claims and maintenance activation take, so it cannot refresh a
  lease into a maintenance capability that activated without counting it.
  Any terminal decision on
  the item, whoever records it, ends its active lease (`state=settled`,
  `end_reason=item-<resolution>`). Moving an item to `pending` (a release,
  with reason `rework`, `partial` or `abandoned`, from `active` or
  `blocked`) likewise ends its active lease, whoever holds it, in the same
  transaction as the status change: `state=released`,
  `end_reason=item-released-<reason>`. The item is claimable at once
  (no `lease-held`, no takeover), the former holder's heartbeat is
  `lease-ended`, and the lease no longer blocks maintenance activation. The
  `item-released` event records the ended ids as `released_lease_ids` (an
  empty list when the item held none). An outcome report awaiting
  verification is untouched and still protects the item. Moving to
  `blocked` is not a release: the lease is kept.
- `work.lease.report-outcome-v1` is an outcome report, not a settlement:
  the worker reports and the record owner settles. `work.lease.complete-v1`
  is its deprecated alias (catalog `deprecation.replacement`), with the
  same input, result and ledger, so one idempotency key is one report
  under either name. The report (outcome, summary, payload up to 64 KiB,
  checks) is always stored on the item, and this authority decides what it
  means. The result's `settlement_effect` says what the report did to the
  work: `settled`, `lease-released`, `parked`, `awaiting-verification` or `none`.
  - `recorded` with `disposition: "parked"` (agentops#2543, operator lease
    point 7): a `failed` outcome may also carry `disposition: "parked"` and
    a `reason_ref` (1-512 characters, no surrounding whitespace; an issue,
    run or report the holder can cite). Both are required together and only
    with `failed` (`invalid-arguments`, 422). A denial is then recorded as a
    fact about the work, not a lease state: the lease is released
    (`end_reason=reported-parked`) as for any failure, and a `work.parked`
    item event names the `reason_ref`, `report_id`, `lease_id` and `run_id`.
    The result's `settlement_effect` is `parked` and the report carries
    `parked: {event_id, reason_ref}`. The item stays `active`, but it is
    not claimable (`work-parked`, 409) and not ready (`get_ready_items`
    skips it even if it was left `pending`) until it is released to
    pending with a reason, which supersedes the newest `work.parked` event;
    a later parked report parks it again. Nothing is stored on the item
    row or in the lease and report tables, so there is no schema change.
    `work.lease.read-v1` reports the current parking as `parked` (`null`
    when not parked). A `rejected` report (superseded, stale or ended
    lease) parks nothing (INV-L1), and `work.parked` cannot be written as
    a generic event.
    Consumer contract (the M3-7 activity and denial report, and any vuoro
    caller that meets a recorded denial): send `disposition: "parked"` with
    the denial's `reason_ref` on the `failed` report, and treat `work-parked`
    (409) from `work.lease.acquire-v1` as a non-claimable refusal, not a
    retryable conflict. Read the current parking from `work.lease.read-v1`
    `parked`. The consuming side lives in the vuoro repository, not in
    sprintctl; this repository only provides the disposition.
  - `rejected`: the lease was taken over (`claim-superseded`), is stale
    (`lease-expired`; a result submitted under a stale or superseded lease
    is kept as evidence but never settles work, INV-L1, and the holder of a
    stale lease nobody took over reactivates it and reports again, under
    a **new** idempotency key: the refused report keeps its key, so
    retrying that key replays `lease-expired`) or
    ended, the item is no
    longer active (`work-not-active`), a succeeded outcome arrives while the
    item waits on an unsettled blocker (`work-blocked`; blockers are
    evaluated at claim and again at settlement), or a succeeded outcome does
    not satisfy the verification profile (`verification-unsatisfied`, 422). The operation fails with that code
    after the report is committed, so the late payload stays as evidence
    and the item does not change. A `claim-superseded` refusal, from any
    lease operation, carries `details`: `{claim_id, current_generation,
    reported_generation}`. The adapter hands `details` to the service's
    rejection when the service's error type accepts them. Released
    vuoro-service (0.1.77) does not yet, so there the generations are in
    the message, and `work.lease.read-v1` lists every lease's `generation`.

    Scope note (agentops#2540 part 2, closed by decision, not built): a
    report on a stale lease that nobody took over stays refused as
    `lease-expired` and is kept as evidence (disposition `rejected`, nothing
    settles). It is not evaluated for settlement, because INV-L1 names stale
    leases: a result submitted under a stale or superseded lease never
    settles work (semantics 3 and 5 in agentops
    `docs/plans/2026-09-27-backlog-ideation.md`, R4; semantics 3 only needs
    expiry alone to be non-fatal to the claim, and the holder gets that by
    reactivating the same lease, Case B). The holder reactivates the same
    lease and reports again under a new idempotency key; the refused report
    keeps its own key and replays `lease-expired`. An implicit
    reactivate-on-report path could be added later, additively. Where the
    vuoro side landed: `lease.py` INV-L1 retention and the shared contract
    section 6 amendment in vuoro#147, and the sprintctl 0.10.0 pin in
    vuoro#148, both in vuoro-service 0.1.78. The edge tool `report_outcome`
    landed in vuoro#150 (vuoro-service 0.1.79 / vuoro-mcp-edge 0.1.6).
    `lease.py` Case B (the same holder reactivates its own unsuperseded
    expired lease with the same `lease_id`) is not delivered in vuoro and is
    carried by agentops#2570.
  - `settled`: a succeeded outcome whose bar needs nothing beyond
    `checks`: no reported check failed, every required check was reported
    passed, and, if the bar includes `checks`, at least one check was
    reported. A failed check or a missing required check rejects the
    report under every profile, `self-reported` included. The authority records an `accept` decision attributed to
    `sprintctl:lease-settlement`, with the rationale "accepted under
    verification profile <profile>" and the report's `payload_digest` as
    evidence; the item becomes done and its dependents can become ready.
  - `awaiting-verification`: the bar needs a verifier role, a separate
    verifier identity or a human (only a contract stored before 0.10.0, or
    a lease pinned under 0.9.0, can still ask for one), and every
    requirement the authority can evaluate is met (a report with no checks
    under a bar that includes `checks` is rejected, not deferred); the
    report waits for a decision, the lease stays, and nobody can claim the
    item meanwhile. Whatever decision next lands on the item -- a served
    `work.decision.record`, `done` as an alias, or an authority outbox
    command, whoever records it -- is stamped on every waiting report of
    the item: a report whose lease was since superseded becomes `rejected`
    with `claim-superseded` (a superseded lease never settles work); on an
    `accept`, a report pinned to a verifier role, verifier identity or
    human (today every waiting report is) becomes `rejected` with
    `decided-accept-unverified`,
    because no decision path checks that requirement yet and the report
    must not read as a verification that happened; any other kind makes it
    `rejected` with `decided-<kind>` (schema 18 ties a `decision_id` to a
    settled report only, so the item's decision event is the link). The
    report and its payload stay either way.
  - `recorded`: a failed outcome; the lease is released and the item stays
    active for the next claim.
- The verification bar comes from the acceptance contracts of every release
  frozen at the item's current revision (`verification_profile`, default
  `checked`; `evidence_obligations` as the required check names). A profile
  is a set of requirements, not a rank (agentops#2539):

  | Profile | Requirements |
  |---|---|
  | `self-reported` | none |
  | `checked` | `checks` |
  | `role-separated` | `checks`, `verifier-role` |
  | `identity-separated` | `checks`, `verifier-identity` |
  | `human-authorized` | `human-authorization` |

  Bars combine by the union of their requirements and required checks, so
  a later reservation can add to the bar but never lower it. A combined bar
  that no single profile names is reported as the named profiles joined by
  `+` (for example `checked+human-authorized`), with its `requirements`
  listed. The lease pins the bar it was acquired under, and settlement uses
  the union of the pinned and the current bar. Only `checks` can be met by
  the holder's own report, so a contract may name only `self-reported` or
  `checked` when it is written; `role-separated`, `identity-separated` and
  `human-authorized` are refused until verifier enforcement exists, and so
  is `self-reported` together with `evidence_obligations`. A stored contract
  naming one of the three (written by 0.9.0), or a stored value that is not
  a profile name at all (which counts as `human-authorized`), fails closed:
  the item cannot be leased (`verification-unsupported`), and a lease
  already pinned under it never settles from the holder's report: its
  report waits, and the next decision on the item ends the wait (an
  `accept` closes the item) while the report is stamped
  `decided-accept-unverified` or `decided-<kind>`, never `settled`. Only the
  Python `reserve(acceptance_contract=...)` path can write a non-default
  contract; without one, every item is `checked`. There is no `parked` lease state: a worker that is denied
  reports `failed` with `disposition: "parked"`, which parks the work, not
  the lease (`work.lease.report-outcome-v1`).
- `work.lease.read-v1` returns the item's current lease (with `stale` as of
  now), every lease (each with its pinned verification) and every outcome
  report, and the item's current verification bar. Any `work:read` caller
  of the repository sees them, holders and payloads included; a repository
  belongs to one workspace.

## Effect intents

Added with remote schema 19 (agentops#2541, M2-1; operator Decision 1 (A) and
INV-E1 on agentops#253). sprintctl owns the effect-intent lifecycle beside the
work, runs and leases it describes. A hosted worker proposes a change to a
repository as a unified diff; a trusted-side principal accepts or rejects
exactly that proposal; a reconciler records that it was applied. sprintctl
applies nothing. The operations are served only by the PostgreSQL authority.

| Operation | Capability (`required_authority`) | Effect |
|---|---|---|
| `work.effect.propose-v1` | `work.effect.propose` | record a proposal bound to the caller's own run and an existing item; idempotent per `idempotency_key` |
| `work.effect.get-v1` | `work.effect.get` | read one intent |
| `work.effect.list-proposed-v1` | `work.effect.list-proposed` | intents still `proposed`, oldest first; optional `item_id` and `limit` |
| `work.effect.accept-v1` | `work.effect.accept` | `proposed` to `accepted` |
| `work.effect.reject-v1` | `work.effect.reject` | `proposed` to `rejected`; requires a `reason` |
| `work.effect.mark-applied-v1` | `work.effect.mark-applied` | `accepted` to `applied`; requires the `commit_sha` and `pr_url` |

- **Separate capabilities.** Each operation has its own capability, none of
  them `work:claim`, `work:write` or another ordinary work authority, so a
  principal that may change work state does not inherit acceptance authority,
  and a worker that may propose cannot accept. The Vuoro service refuses a call
  whose identity lacks the operation's `required_authority`;
  `WorkApplication.invoke` checks it again and answers `authority-required`
  (403), failing closed for an identity that carries no authorities.
- **Revision and digest.** An intent carries `revision` (1; a change is a new
  proposal, never a new revision of an accepted one) and
  `canonical_intent_digest`: the lowercase sha256 of the canonical JSON of the
  item, repository, base commit, title, rationale and diff
  (`sprintctl/effect_intent.py:canonical_intent_digest`). The authority
  computes it. It does not cover the intent id, run, proposer or idempotency
  key, so the same content proposed under two keys has one digest and is two
  proposals. The proposer is the authenticated principal.
- **Compare-and-set.** `accept`, `reject` and `mark-applied` name
  `intent_id`, `revision` and `canonical_intent_digest`. Under the row lock the
  authority checks the revision (`effect-revision-mismatch`, 409), then the
  digest, which must equal the stored one and the digest of the stored content
  (`effect-digest-mismatch`, 409), then the state (`effect-invalid-transition`,
  409). A refused call changes nothing, and of several racing acceptances
  exactly one wins. An unknown intent is `effect-not-found` (404).
- **Acceptance record.** `intent.acceptance` is `{intent_id, intent_revision,
  canonical_intent_digest, acceptor_principal, acceptor_policy_version,
  accepted_at}`. The acceptor is the authenticated principal, never a wire
  argument: naming `acceptor_principal` or any other field the operation does
  not define is `invalid-arguments` (422). `acceptor_policy_version` is null
  until a policy acceptor exists. `mark-applied` keeps the record unchanged.
- **Immutable once accepted (INV-E1).** Storage refuses, by trigger, any update
  that changes an intent's content or an accepted intent's acceptance record,
  any update that is not a legal transition, and any move out of `rejected` or
  `applied`. A change to an accepted proposal is a new proposal with a new
  intent id; the accepted one stays as it was. Deleting the item or run an
  intent belongs to cascades to it, as for leases.
- **Not settlement.** Accepting an intent never changes the work item or its
  lease, and settling the work never accepts an intent.
- **Proposals.** `propose-v1` refuses a run that is not the caller's
  (`run-not-found`, 404, the same code for an unknown run) and an item the
  repository does not have (`work-not-found`, 404). A replay of the same key
  and content answers with the intent as it is now, not as first proposed; the
  same key with other content is `idempotency-conflict` (409).

## Authority and retry semantics

Reservations and lifecycle transitions accept the existing immutable
authority-command producer record. Before arbitration, the application
reparses the nested command and requires its canonical form. The outer record
actor, nested command actor and authenticated identity must match; for
`claim.acquire`, the requested reservation agent must match them too. A
single-command invocation's basis revision and idempotency key must match the
canonical record. The v3 reservation model removes bearer-token proof: local
CLI reservations are credential-free and served arbitration resolves proof
through authenticated identity, not client-supplied secrets.
`work.identity.current` returns only the authenticated work actor and
repository scope. Served lifecycle clients use it before minting a durable
command so a local OS username or operator-supplied label cannot create a
permanently unflushable actor-mismatch record; credentials and token material
are never returned. Served `reservation reserve`, `reassign` and `release` use
it the same way: `--actor` defaults to the authenticated actor, and a different
value is reported on stderr and not sent. `sprintctl doctor` shows that actor
(`identity: actor=...`; `schema.authenticated_actor` in `--json`), and a
reservation `actor-mismatch` rejection names both the given and the
authenticated actor.

For `work.batch.apply` only, an already-durable authority command with an
actor or reservation-agent mismatch is admitted in its producer order and
receives a durable `command.rejected` decision with reason `actor-mismatch`.
This consumes the immutable origin sequence without applying a domain effect,
allowing the next record to replay. Direct single-command operations still
reject the same mismatch before authority admission; the batch exception
exists solely for recovery of an existing ordered producer log.

`work.claim.start` is the transitional Click-free equivalent of the legacy
one-shot command: it creates an execute reservation for the authenticated
actor and moves a non-active item to active. Because this composition has no
durable request ledger, its catalog contract forbids idempotency keys and
callers must not retry an unknown outcome. Retry-safe shared-authority clients
use an immutable `claim.acquire` record with `work.claim.arbitrate` instead.

The application delegates arbitration to `sprintctl.authority`. PostgreSQL
records the request and accepted or rejected decision atomically. Repeating an
identical record returns the original decision with `duplicate=true`; reusing
its stream position or event ID for different content is rejected. Stale basis
is a durable domain rejection and never mutates the target.

Evidence ingestion delegates to `sprintctl.pg.ingest_records`. A record batch
keeps producer order: adjacent observations are admitted atomically, each
authority command is decided at its position, then the next observation run
is admitted. Batch keys are content-bound, and record-level admission makes a
retry after a partial or lost response safe. A batch may carry multiple
command basis revisions by omitting the invocation-level basis; each immutable
command retains its own required basis revision.

Project batches follow the project binding's declared member order. Each
member stays repository-scoped and writes only the work domain. The response
retains `origin_repo` and exposes each member result. The operation is
retry-safe at the record level, not a cross-repository transaction.
Repository-local ingestion cursors mean two member results may carry the same
numeric `ingest_offset`; the enclosing member `origin_repo` / `repo_id` is part
of that cursor identity and must be retained by clients.

Concurrency evidence is deliberately bounded: PostgreSQL reservation
arbitration locks the authoritative work-item row. Independent connections
demonstrate that overlapping reservation commands are durably recorded and
surfaced as visible conflicts. This is `concurrency-tested`
application-invariant evidence, not a general fencing or cross-operation
linearizability claim.

## Maintenance capability boundary

The served maintenance operations expose the lifecycle owned by
`sprintctl.maintenance_capability`; they do not introduce a second state
machine. Preparation accepts the complete frozen `maintenance-envelope/v1`
and derives the operator from the authenticated invocation identity. The
catalog rejects unknown top-level envelope and operation fields, while the
domain validator remains the normative exact validator for every nested
contract object. Client-supplied authority time is not accepted: the
application supplies its trusted current time to the lifecycle store.

`work.maintenance.transition` exposes only the lifecycle actions `attest`,
`activate`, `observe`, `reconcile`, `abort`, and `revoke`. Every mutation binds
the Vuoro invocation idempotency key to its request ID. The semantic request
digest deliberately excludes the server observation time, so retrying the
same request after response loss returns the first receipt as a duplicate;
changed bytes under the same identity are rejected. Expected revisions remain
the capability store's compare-and-swap tokens and stale revisions produce a
stable `maintenance-revision-conflict` response.

The read projection returns lifecycle identity, state, revision, sequencing,
and timestamps only. It does not expose the frozen envelope bytes, receipts,
reconciliation bundle, or recovery records. Recovery callers require the
narrow `work:maintenance-audit` authority. Their `observation` and
`requested-command` records are append-only audit evidence and never grant or
exercise execution authority. Repository scope is inherited from the served
application binding on both SQLite and PostgreSQL.

## Transitional CLI parity inventory

The local command surface uses `sprintctl reservation`, and the served catalog
now names those operations `work.reservation.*`. The historical `work.claim.*`
labels were retired with the v2 catalog cutover; they are not accepted, and
nothing dual-registers them.

| Current local surface | Served operation |
| --- | --- |
| `sprintctl sprint list --json` | `work.read.sprints` |
| `sprintctl item show --id ID --json` | `work.read.item` |
| `sprintctl item edit --id ID --description TEXT` | `work.item.edit` |
| authenticated durable-command actor discovery | `work.identity.current` |
| `sprintctl next-work --json` | `work.read.next-work` |
| `sprintctl reservation reserve` | `work.reservation.reserve` |
| `sprintctl reservation touch/reassign/release` | `work.reservation.touch`, `work.reservation.reassign`, `work.reservation.release` |
| `sprintctl item status` and `sprintctl sprint status` | `work.lifecycle.arbitrate` |
| observation upload | `work.evidence.ingest` |
| authority synchronization | `work.batch.apply` |
| project next-work and dispatch ordering | `work.project.next-work`, `work.project.batch` |
The inventory is also machine-readable as
`sprintctl.vuoro_adapter.LEGACY_REMOTE_COMMAND_PARITY`. It is reservation-parity
evidence, not authorization to remove direct mode. Endpoint/identity cutover
and backend retirement remain separate governed items.
