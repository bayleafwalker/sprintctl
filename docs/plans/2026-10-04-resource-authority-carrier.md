# Resource authority carrier: bounded design for agentops#2603

Status: proposed owner contract, not implementation or qualification. No schema,
capability grant, runtime operation or release is added by this document.
Governing outcome contract: Vuoro
`docs/plans/2026-10-03-resource-authority-contract.md` at
`9c02d8f9b7cc42f9b90c4a302b623b66d54b4266`.
Sprintctl 0.12.0 remains unqualified for that complete resource contract.

## Problem and placement

Authenticated published-owner counterexamples show that an epoch-one principal
with repository write authority can edit an epoch-zero-created work item, and
opposite dependency edges both commit. These are team coordination semantics,
not evidence that an existing generic resource owner has violated its own
contract. Work-item write/lifecycle grants must retain their existing meaning.
`work.maintenance.resource.*` observes maintenance capabilities; `work.effect.*`
owns immutable proposed intents. Neither becomes a generic resource aggregate by
an alias or an adapter-local model.

The proposed carrier is a separate repository-scoped resource authority owned by
Sprintctl and exposed through its served adapter. It stores coordination facts.
Protected-horizon authorities continue to own evidence-byte verification,
digest-bound acceptance, signing, merge and effect application. A local recorded
acceptance or settlement fact does not perform those actions or settle a work
item. The proposed first implementation uses authenticated narrow trusted-side
transport. A later signed-assertion alternative needs a separately versioned
contract. The accepting/reconciling principal is the authenticated transport
principal, never a caller-selected reviewer string. The transport authenticates
each reviewer/reconciler end principal directly; a shared service principal with
a caller-supplied acting reviewer is not an eligible review path. The protected
owner's own service identity may authenticate byte-verification responses but is
not substituted for the reviewing principal.

This design deliberately does not use `work:write`, `work:lifecycle`, lease
ownership, an assignee, an actor display name or a maintenance grant as resource
mutation authority. Principal identity is authenticated `issuer:subject:epoch`;
reissuing an actor string never transfers a resource. No transfer operation is
proposed.

## Proposed operations and capability separation

Names below are candidate owner operation identifiers, not a requirement that a
challenger copy this API. They must become versioned catalog contracts only after
implementation, independent review and publication.

| Candidate operation | Separate proposed capability | Additional conditions |
| --- | --- | --- |
| `work.resource.create-v1` | `work.resource.create` | Expected absence/revision zero; creator from authenticated principal |
| `work.resource.get-v1` | `work.resource.read` | Repository authorization and resource visibility |
| `work.resource.changes-v1` | `work.resource.read` | Same visibility; contiguous history, bounded cursor reads |
| `work.resource.relate-v1` | `work.resource.relate` | Own source, exact revision, visible target; mixed cycle check |
| `work.resource.reference-v1` | `work.resource.relate` | Own source; typed external assurance, never a lease |
| `work.resource.record-evidence-v1` | `work.resource.record-evidence` | Valid source state; repository-visible source and protected-owner byte-verification receipt |
| `work.resource.record-acceptance-v1` | `work.resource.record-acceptance` | trusted-side authenticated transport; non-creator reviewer; exact reviewed binding |
| `work.resource.record-rejection-v1` | `work.resource.record-rejection` | trusted-side authenticated transport; non-creator reviewer; projection digest; evidence optional |
| `work.resource.record-settlement-v1` | `work.resource.record-settlement` | Trusted reconciler; accepted/rejected state; local fact only |
| `work.resource.supersede-v1` | `work.resource.supersede` | Own source; exact revision; terminal |
| `work.resource.command-decision-v1` | `work.resource.read-decisions` | own authenticated principal's decision only |

`work.resource.audit-decisions` is a separate proposed operator-only capability,
with a separately cataloged audit operation. It is not implied by read-decisions.

Every mutation additionally requires `work.resource.read`, including create;
mutation grants alone do not reveal resource existence or graph paths.
Mutating operations require an explicit idempotency key and expected resource
revision (zero only for creation). Create accepts neither caller-selected creator identity nor a resource identity.
The owner mints the opaque resource identity and atomically proves absence before
revision one; expected revision is zero. A same-key replay recovers that original
identity from the permanent first binding. A forced mint collision must never
replace an existing resource. No future ledger-expiry behavior is included. Read grants never imply mutation, and any mutation grant never implies
another. A trusted role is a bound identity plus a narrow grant, not a user field
or the existence of a hosted session. New capabilities default to absent in all
existing catalog/Cloud mappings. There is no deployment claim until the separate
release, pin and rollout requirements are fulfilled.

## Aggregate and authoritative journal

Persist opaque identity, immutable creator principal, revision, state, relations,
typed references, evidence bindings, acceptance/rejection facts and local
settlement facts. The resource projection is reconstructible from immutable
changes. Accepted mutations atomically perform exact-revision CAS, append one
change and advance revision once. Refusal changes neither projection nor change
journal. Work items, work decisions and leases are not mutated by these commands.

The states and allowed transitions are the governing neutral contract:
registered → evidence-recorded → accepted; registered/evidence-recorded may
instead become rejected; accepted/rejected may append a settlement fact without
changing that state; any non-superseded state may be superseded. Relations and
typed references may be added to any non-superseded state. All commands compare
the current revision. Acceptance stores resource identity, reviewed revision,
reviewed canonical resource-projection digest and accepting immutable principal, and must verify the protected
owner's current evidence availability/verification binding. Evidence records have
immutable binding IDs, byte digests (SHA-256 of the exact evidence bytes), typed
owner references and a canonical binding-record digest. Multiple records may be
added while evidence-recorded; acceptance names one exact binding ID, its record digest, its byte digest and
the reviewed resource revision. Rejection from registered or evidence-recorded
binds the reviewed canonical resource-projection digest and revision; it may
cite an exact evidence binding but does not require one to reject. The
transport must synchronously establish byte availability/integrity at admission;
a cached previous verification alone is insufficient. A changed or missing byte binding refuses evidence recording or acceptance
without advancing the resource. Rejection cites binding ID/record digest only
when present in the reviewed projection; it never requires byte reverification
and never implies those bytes are available or verified.

Evidence attachment deliberately permits any same-repository authenticated
evidence-ingester with its separate grant; source ownership is not required.
It changes the source revision through CAS, so concurrent review cannot accept
the previously reviewed revision after a new attachment. Acceptance and rejection use the same narrow reviewer-authenticating transport
and require a non-creator reviewer under `issuer-subject-separated/v1`: compare
the authenticated issuer and subject with the creator, excluding the epoch.
Rotating one's own epoch therefore never enables self-review. This is a declared
principal separation policy, not proof that two subjects represent different
humans or organizations. Reviewed revision must equal the CAS expected revision;
it cannot approve old content after a newer mutation. Rejection is
terminal apart from supersession. An invalid/missing evidence binding cannot be accepted. A rejection never
implies evidence verification or authorizes an effect; its reviewed projection
digest must match even when no evidence is cited. Settlement is an
append-only series of local facts, each independently idempotent, recorded only
through the same narrow trusted-side transport by its authenticated reconciler.
Later relations/references advance the revision but remain outside the accepted
binding: an accepted state never implies approval of revision N+k. Readers must
inspect the exact acceptance/rejection binding, not just the state label.

Journal positions are per resource, equal its revision, and contiguous 1..N.
Allocate the next position from the locked source revision within the same
transaction; no nontransactional sequence allocates these positions. Cursors
bind repository, resource identity and position, not a global repository offset.
Canonical content digests bind each change and its preceding digest (genesis is
explicit). Rebuild also compares its final revision/digest to the locked current
projection anchor; a truncated tail is refused, not mistaken for an old snapshot. Rebuild rejects gaps, duplicate positions, digest conflicts, identity
changes and creator changes. Native facts and imported provenance remain
explicitly distinct. No pruning or checkpoint/tail compaction is included.
Import and retention authority require separately approved owner contracts.
The ownership view is derived from immutable creators, not a mutable side table.

## Relations and concurrency

All relations are directed source → target, including `parent-of` and
`depends-on`. This adopts the harvested freeze's directed mixed-cycle outcome;
it does not redefine existing work-item dependencies. Self relations are refused.
A new `parent-of` or `depends-on` edge must not close a path through either kind.
`derived-from` and `supersedes` retain typed provenance semantics and never
transfer ownership. Only the source revision changes; the target stays unchanged.

The proposed first boundary is one graph-admission lock per repository, shared
by every parent/dependency relation admission. Targets must be in the same
repository; cross-repository targets are refused. PostgreSQL uses READ COMMITTED,
acquires a transaction-scoped advisory lock derived from the exact repository
identity as its first statement, then performs source CAS and graph reads in later statements. Within a cycle-forming command's mutation
transaction, even replay/capacity reads occur after that first graph-lock statement.
The lock order is graph admission (when required), repository/principal quota,
first binding, then source row. All resource commands follow the same applicable
order; no source-first or binding-first path later acquires a graph lock.
Credential/framing admission checks outside that transaction read no resource
graph. Reads before this lock or a snapshot acquired before a lock wait are forbidden. SQLite uses BEGIN IMMEDIATE before any admission read.
Lock-key collisions may serialize unrelated repositories but must never remove
serialization within one repository. Backend tests assert the declared isolation
and an independent-session blocked-lock history, not just serial calls.

Relations to superseded targets are allowed as historical references. Target
state therefore is not an admission condition. Superseding a resource changes
neither its incident relations nor the graph and needs source CAS, not the graph
lock. Source relation versus source supersession is serialized by that source's
CAS lock; one loses without a partial change. Cycle checks include superseded
resources' retained edges. Derived-from and supersedes relation cycles are
permitted provenance structures; the supersedes relation does not perform the
terminal supersede command. No edge is deleted by this initial contract.
A later finer-grained admission algorithm requires new independent histories.
Settlement after supersession is refused under the frozen terminal-state contract.
Late native outcomes belong to the protected owner's append-only outcome evidence,
not a revived hosted resource mutation; any successor reference requires ordinary
source ownership. Implementers must cite and verify that outcome carrier before
claiming recovery of already-applied effects; no such recovery is inferred here.

## Independent rejected-command decision ledger

The first binding key is `(environment, repository, immutable principal,
operation, idempotency key)` plus its immutable canonical request digest.
Canonical JSON is compact, sorted and Unicode preserving; floats/NaN are refused.
Exact replay returns original response bytes even after a lost response or
process restart. A conflicting digest has its own immutable refusal keyed by the first binding
plus conflicting request digest; exact conflicting replay returns its original
bytes. It never replaces the first binding or executes a mutation.

Concurrent same-key commands serialize on the binding uniqueness/admission lock.
The loser rolls back its entire attempted transaction, re-reads the committed
winner and returns those original bytes for an equal digest, even if its own
resource CAS would now fail. Unequal-digest losers commit only their separate
conflict decision. No loser's projection or journal mutation survives.

Storage admission is bounded per authenticated principal/repository by an
explicit configured byte/record capacity, including conflict decisions, plus a
repository/environment aggregate limit so epoch rotation or many members cannot
reset the total bound. Capability-denial decisions count toward both limits.
Consequently an admitted member without resource grants can consume its quota,
and enough admitted members can exhaust the aggregate and deny new admissions
for everyone until an operator raises capacity. This explicit availability tradeoff
must be reviewed before implementation; it is not deployed by this proposal. There
is no eviction of first bindings or refusals at all in this initial design: all admitted decisions remain permanently.
A filled quota therefore remains unavailable for new admissions until an
authorized operator explicitly raises its capacity; rotation is not a reset. Exhausted capacity makes the owner unavailable for new admissions:
return a transient service-unavailable transport result before making an
authoritative accepted/rejected decision, and never execute a mutation. Exact
existing replay remains available. This is a readiness/availability outcome,
not a replayable domain refusal or silently discarded decision. Implementation
must expose this boundary and prove no admission happened on overflow; choosing
capacity/retention cannot authorize deletion. Every admitted conflicting digest
therefore has a durable refusal; unadmitted traffic cannot grow that ledger.

Every accepted or rejected decision preserves request digest, status, code/message,
response bytes and response digest, resource identity and before/after revision.
A refusal may have no disclosed resource revision; private authoritative fields
must not leak through public refusal bytes. Decisions are independent of resource
change history. Accepted mutation, decision and projection changes commit in one
transaction. Rejected CAS/state/content decisions commit without resource changes.
A response is returned only after its decision is durable.

Before any ledger schema is implemented, the proposed versioned admission
contract must be reviewed jointly with the Vuoro shell owner. A resource command
is admitted only after authentication, environment binding, repository
membership, a known operation, a valid key and the framing/size checks. Outside
that boundary (unauthenticated, wrong environment or foreign repository), the
shell returns its existing non-replayable transport refusal/audit; no target
repository command decision or resource exists. These are not admitted resource
commands, and must never be reported as durable command-decision support.

After repository admission, a missing resource capability on a mutating command
is journaled in the repository ledger, privately scoped to the authenticated
principal. Reads have no key and retain ordinary non-replayable transport denials;
this is not advertised as a command-decision receipt for a read. Capability checks
precede argument schema validation. The framing classifier is independent of that
authority decision: canonical representable admitted bodies use canonical framing,
noncanonical invalid arguments use bounded raw framing. No argument validation
or resource lookup discloses an existence/revision before read authority.
A first-binding capability refusal continues replaying after a new grant: the
caller must use a new key for a new attempt. The proposed
integration is a registered internal owner refusal recorder invoked by the shell
with its own typed authenticated decision context before a denial response. It
cannot dispatch a mutation or receive caller-authored refusal payloads. Failure
to commit that decision fails closed as service unavailable; it does not return
an authoritative refusal that was never recorded. This is an internal owner
integration, not a new caller capability or a bypass of ordinary authorization.
Its shape and denial/refusal privacy require a separate jointly reviewed source
plan before schema or handler work.

Valid requests use the canonical request framing already specified. For a
parseable admitted envelope with a known operation and valid key but invalid
arguments, the refusal framing records `raw-json/v1` plus SHA-256 of the exact
bounded UTF-8 request body. This is explicitly not canonical resource content:
invalid floats/NaN never enter a resource. Exact invalid-byte replay is stable;
changed raw bytes under the same key conflict with the first binding. If the
outer envelope cannot supply validated operation/repository/key, it never enters
the command ledger. Canonical and raw framing tags are included in request digest
identity so invalid and valid commands cannot share a binding accidentally.
Current protocol-v1 behavior does not provide this proposed admission integration;
full resource qualification remains unavailable until it is implemented and tested.

## Proposed read privacy

Visibility is repository-wide for an authenticated `work.resource.read` holder,
including retained graph edges. No narrower hidden subgraph is introduced by this
first contract. Resource get/change responses expose immutable creator and
reviewer/reconciler principal IDs to that role deliberately; they never expose
credentials, transport assertions or private denial metadata. Without that grant,
forbidden and nonexistent resources have the same fixed non-disclosing response.
Cycle validation runs only after repository/resource grants and sees the whole
same-repository graph; it reveals no foreign repository or hidden subgraph.

`read-decisions` exposes only the caller's original public response bytes, response
digest and status. It never returns private stored resource/revision fields from
refusals. The separately named audit capability may expose authoritative decision
metadata only through its explicit operator contract. Epoch rotation intentionally
does not transfer resource mutation or old-principal decision access: the new
epoch cannot read old-epoch private decisions. Repository read access may still
read old resources, and an explicitly authorized audit owner can retain evidence.
No rescue/ownership-transfer operation is silently introduced.

## Acceptance matrix and publication sequence

| Essential outcome | Required meaningful proof |
| --- | --- |
| Identity/CAS | Create absence/0 → 1; duplicate creation refused; two independent same-revision writers produce one mutation; monotonic revisions prevent ABA |
| Ownership/ACL | Epoch-one identity cannot mutate epoch-zero source despite identical actor; every capability cross-denial; creator supplied in input rejected |
| Relations | Cross-repository targets refused; declared-isolation lock wait; relation versus source supersession and relation to superseded target; source-only revision; target unchanged; self/mixed cycles refused; independent reverse and disjoint mixed-edge races serialized without partial changes |
| Decisions | Equal-key/equal-digest and equal-key/unequal-digest independent-connection races; accepted/rejected exact-byte replay after process restart; conflicting digest refusal cannot replace first binding; denied authority/invalid framing follow their documented integration contract |
| Atomicity/recovery | Fault between projection/change/decision rolls back all accepted facts; rejection commits decision only; journal rebuild detects each distinct corruption and tail truncation against the projection anchor |
| Reviewer/evidence workflow | Registered rejection without evidence succeeds; mismatched projection digest and absent cited binding fail; stale reviewed revision fails on both paths; rotated-epoch self-rejection and shared-service acting reviewer fail; non-creator ingester attachment succeeds but that grant cannot relate/supersede |
| Evidence/acceptance | Missing/corrupt evidence before record or accept refused; exact identity/revision/digest verification; same-subject rotated-epoch self-review refused; shared service acting-reviewer input refused; exact evidence-binding ID and record/byte digests; post-acceptance revision is not accepted content; no hosted signer/apply grant implied |
| Settlement/supersession | Late settlement after supersession is refused without fact loss in the separately cited protected outcome owner; accept/reject/local settlement never changes work-item status; state remains accepted/rejected on settlement; supersession terminal and creator unchanged |
| Mutation read gate | Every mutation grant without resource.read receives the fixed non-disclosing denial, including create |
| Minted identity | Caller resource ID refused; forced mint collision leaves original unchanged; equal-key replay after restart returns original minted ID |
| Read privacy | Reader and decision access confined to repository/principal scope; forbidden and absent objects return the fixed non-disclosing surface; own decision reads never expose private refusal resource/revision fields; foreign denials never write victim-repository rows |
| Capacity/mapping | Epoch rotation and multiple principals cannot reset/exceed aggregate capacity; capability-denial decisions count toward both quotas; no mutation or authoritative decision on admission exhaustion; original replay survives; every existing exact/prefix/wildcard mapping is checked to prove resource caps stay absent |
| Derived ownership | Restarted authoritative projection yields immutable creator view; no grants/leases/assignees/acceptors confer ownership |

Use temporary SQLite and disposable PostgreSQL only; each concurrent actor uses
an independent connection with a barrier immediately before admission. Preserve
minimized traces and exact refusal codes. Bind the neutral oracles through the
published served adapter and real HTTP authorization, not a private direct
handler that bypasses shell guards. Gaps stay separately marked and filed.

A design PR is not acceptance of agentops#2603. Joint shell admission/refusal integration and protected-transport contract review
precede any ledger schema decisions. Implementation/schema decisions,
owner tests, independent public review and immutable owner release precede the
exact consumer pin and published-runtime conformance gate. Cloud mapping and
production rollout remain separate items. At every intermediate gate report
what is implemented, what is published, and what is actually served. Do not remove
the harvested gap receipts until the corresponding neutral scenario passes and
the qualification inventory is updated with its immutable receipts.
