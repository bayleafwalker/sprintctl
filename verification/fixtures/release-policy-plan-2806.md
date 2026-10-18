# Proposed #2150 child: share private Release policy and basis/current query shapes

**PROPOSED, unregistered; no source or native mutation.** Source inspected at accepted `1ec78fa9a2f1dbacc4d5ec832c14b961e09abc12` (#2805 accepted369). Parent #2150 stays open. Native parent5809 records residual families. This is source reuse with behavior frozen, not a demonstrated product defect or new Release feature. Coordinator owns the actual-backend oracle, product edit and qualification.

Use dispatch-plan and verify-state-protocols Depth2. The existing SQL-free `releases.py` remains the canonical values/digest/contract layer. **Decided module:** add private `sprintctl/releasecore.py` for the three orchestration rules plus the shared basis and current-selection query shapes. It may import `releases`; it receives the description-revision callable, avoiding a new dependency on a backend or moving `_description_revision`. No public repository/provider interface or general driver is introduced.

## Exact dispositions

| Family | This child | Responsibilities retained in backend |
| --- | --- | --- |
| `_release_basis` / `_release_basis_locked` | One private basis query shape owns item projection and correlated ITEM_EDITED/revise counts; one basis-derivation helper handles missing item, sequential metadata extraction/`int` conversion, dynamic description-revision call, then `release_revision`, returning the same item/revision/count tuple. | Execute/fetch, event discriminator argument, actual optional PG locking input, tenant/item parameters, fetch timing, and exact `dict(row)`/`_norm(row)` conversion. No conversion of the input row until the original point after fetch; missing row stays None. |
| `_freeze_release` / `_freeze_release_locked` | One private freeze sequence canonicalizes already-read refs, computes digest, then invokes one noncommitting backend insertion callable with a once-only value supplier. Only after the backend binds execute does that supplier access insertion item ID and canonicalize contract then refs. Returns the original digest. | Ref SELECT and actual fetch order; SQLite dict conversion versus PG raw mappings; original SQL `INSERT OR IGNORE` / `ON CONFLICT(repo_id, release_digest) DO NOTHING`, JSONB casts, item/repo scope, actor, transaction and cursor. |
| `_decision_release` / `_decision_release_locked` | One private default/membership policy: `None` alone reads current and returns digest or None; every explicit value uses the membership callable, returns the original value or raises the same ReleaseMismatch/message. | Existing same-item membership SELECT, tenant scope and fetch; current-read callable invokes the existing backend current helper dynamically. No new digest validation or currentness requirement for explicit historical membership. |
| `_current_release_row` / `_current_release_locked` | One **query-shape** helper owns revise-count equality, correlated reservation MAX(id), DESC NULLS LAST, Release-id tie-break and LIMIT1. It accepts trusted internal placeholder and tenant-scoping constants, returning SQL only. | Execute/fetch/decode and params unchanged: SQLite `(item_id,)`, PG `(repo_id,item_id)`; PG tenant predicate in outer Release plus both correlated subqueries. No new lock, cursor, commit, Python sort/filter, validation, or extra query. |
| `add_dep` duplicated admission guards | Explicit separate follow-up candidate under #2150, not part of Release child. Still a material small policy fork. | Must later retain self-equality -> blocker existence -> blocked existence -> shared insert ordering and exact errors/types/ownership. This child does not fulfill or exempt it. |
| PG imports ref/sprint pure rules from db | Explicit separate ownership/disposition review, no relocation here. One implementation shared through a backend peer is different from duplicate code. | Existing imports/reexports and public error/class identities remain. Any move needs its own compatibility scope; parent closure requires a reasoned disposition rather than counting it complete silently. |

The basis metadata query and current-selection query are therefore **not silently deferred** or excused as SQL: their shared helpers own the count/selection rules. For basis, use `_basis_query(*, ph, tenant_scoped, lock_item)` with trusted internal backend arguments: both correlated event/Decision counts gain the PG repo correlation only in tenant mode, event_type stays the first bound parameter, and backend args remain SQLite `(ITEM_EDITED_EVENT_TYPE,item_id)` versus PG `(ITEM_EDITED_EVENT_TYPE,repo_id,item_id)`. Append `FOR UPDATE OF wi` only for the original PG truthy `lock` argument; SQLite passes false and gains no guard. No new lock type validation. For current, its domain selection text becomes one shared query shape here, with real backend selection tests. No generic SQL abstraction or fusion with basis reads. Keep the other Release public read/list/commit helpers unchanged; record their remaining storage-only wrapper inventory explicitly at closeout.

## Fixed private interfaces

Use these private seams in releasecore (annotations describe existing values, not new runtime validation):

- `_basis_query(*, ph: str, tenant_scoped: bool, lock_item: bool) -> str` and `_current_release_query(*, ph: str, tenant_scoped: bool) -> str` return SQL only, receiving backend constants/current lock argument. No connection/store/driver objects.
- `_basis_from_item(item: dict | None, item_id: int, *, description_revision: Callable[[dict, int], str]) -> tuple[dict, str, int]`; backend converts a present row exactly once beforehand, passes None otherwise. Callable uses dynamic backend lookup when invoked. The helper owns missing-item refusal and all postnormalization metadata/revision sequencing.
- `_freeze(item: dict, revision: str, revise_count: int, *, actor: str, acceptance_contract: dict, refs: Iterable[Mapping], insert: Callable[[Callable[[], tuple[int, str, int, str, str, str, str]]], None]) -> str`. Insertion receives a once-only supplier returning `(item_id,revision,revise_count,digest,contract_json,refs_json,actor)`; backend closes over original connection/cursor/repo and calls `conn.execute(SQL, values_supplier())` or `cur.execute(SQL, (repo_id,) + values_supplier())`. This binds execute before insertion serialization. Refs were fetched/converted before calling; no insertion callback may add reads or commits.
- `_resolve_decision_release(item_id: int, digest: str | None, *, current: Callable[[], dict | None], member: Callable[[str], bool]) -> str | None`. Current callback calls existing backend current helper dynamically; membership callback executes original SELECT and returns `fetchone() is not None`. Callback annotations do not normalize explicit input types or falsey values.

No participant dataclass/Protocol needed for these one/two-callback seams; this is not a general composition API. All original backend helper wrappers remain at the same caller boundaries. If implementation needs an extra decision, return to review before expanding these seams.

## Frozen sequencing and call boundaries

Keep all eight renamed backend helper names, signatures and dynamically callable entrypoints. Existing reserve/Decision/effect/native callers do not bypass them. Resolve backend globals at their original invocation: SQLite uses its `_description_revision` alias; PG uses `_workitemcore._description_revision`; do not replace either with an eager shared import. A forwarding lambda lets shared derivation call the correct dynamic lookup after metadata pops/conversions, preserving monkeypatch/observer behavior. `release_revision`, canonical helpers and current backend helper remain runtime lookups, not import-time captured defaults.

Basis wraps the backend-normalized *new* dict as before, then pops edit_version before revise_count, invokes description revision only afterward, and returns that same dict. Preserve None/error precedence. Do not introduce copying, schema checks, item-ID coercion or boolean normalization.

Freeze must preserve argument evaluation, not just final digest: original ref query accesses item ID before canonicalization; after `release_digest` the insertion tuple accesses item ID again before contract serialization and refs serialization. Existing release_digest itself calls canonical_json(document); preserve that internal call as well. The core owns tuple construction in a supplier consumed exactly once inside the backend execute argument expression. SQL execute attribute lookup occurs after digest-document canonicalization, before item ID is read again and before insertion-contract and refs serialization. The backend binds execute before calling the supplier; actual SQL invocation follows all serialization. No eager pre-serialization before execute binding, repeated supplier calls, or serialization after actual SQL is allowed. Ref selection/conversion remains before invoking shared freeze. It performs no new acceptance normalization. Actor and original serialized contract survive duplicate digest insertion; no overwrite/upsert update. PostgreSQL JSONB storage is compared structurally: observed canonical insertion strings are not a claim of JSONB on-disk byte identity.

None/default and explicit membership paths remain disjoint: no eager current read on explicit input; do not turn empty string or other falsey explicit value into default. No new lowercase/format/type normalization. Historical same-item Release membership continues to pass this *helper* even where later native constraints enforce narrower Decision admission. Tests must distinguish private helper membership from public Decision acceptance, preserving both.

Current means current revise count, then latest reservation **ID referencing a digest**, irrespective of reservation state/role, then Release row ID, including rows with no reservation after referenced rows. It is not latest timestamp or latest inserted Release. Count revise Decisions only; no edit-only currentness tightening. Both SQL subqueries remain scoped exactly as now. No lock is added despite PG helper name. Basis/current are still separate reads, with no new shared-snapshot guarantee.

## Protected callers and intentional differences

Callgraph is recorded in the supplement and source pins (static calls can miss aliases). Freeze callers remain reserve-admission participants. Basis callers include item_release_revision, both reserve admissions, PG readonly effect_proposal_release_basis/evaluate_evidence_snapshot (`lock=False`), effect_causal_admission, `_effect_release_locked`, and `effect_attempt_pg._permission_guard`. Current helper also serves public current_release, effect proposal/admission/acceptance and evaluation. Decision resolution remains reached through `_decide_locked`, including direct/keyed/authority/legacy/native paths; accepted #2805's writer order/opaque insert handle is untouched.

SQLite only resolves nonexecution reservation basis when explicitly requested; PG reads and locks basis for all roles before the shared admission. Retain this distinction. PG item locking, repo admission advisory lock, maintenance gating, hosted keyed ledger, row normalization, timestamp types, native lease/effect guards and tenant isolation stay unchanged. Readonly exports never acquire the PG write lock. Existing public caller-transaction policies (including SQLite idle refusals and PG native participants leaving owner effects pending) stay outside releasecore. No new commit/rollback/begin/savepoint/retry/clock/event hooks.

## Coordinator-owned oracle BEFORE product edits

Author one shared actual-backend contract plus SQLite temporary DB and disposable PG adapters, e.g. `tests/_release_policy_contract.py`, `tests/test_release_policy.py`, `tests/pg/test_release_policy.py`. It calls **existing renamed backend helpers and public entrypoints**, never the future core. Freeze exact baseline source/test/helper/file hashes, actual invocations, raw JUnit/stdout and case expectations before implementation. Full normalization of outputs may exclude only documented backend IDs, repo field and native timestamp representation; assert those separately. Store all actual mutations in temporary/private fixtures, never live backend. Do not claim new oracle already ran.

| Gate | Actual before/after observations and discriminating error |
| --- | --- |
| B1 basis | Missing item exact ValueError; description None/Unicode; independent ITEM_EDITED count vs unrelated events; revise vs valid non-revise accept/reject/withdraw/supersede Decision counts; note is an unrelated event, never a Decision kind; expected revision literal from fixed UUID/description and stdlib test-owned SHA calculation; resulting tuple types and popped metadata. Controlled dynamic forwarders prove normalization then edit/revise conversion then original description helper and release_revision. Competing malformed metadata cases preserve conversion/error order; these narrow synthetic row tests supplement real SQL cases, not replace them. |
| B2 locking/read ownership | Existing PG held-item-lock + pg_blocking_pids exact writer proves default basis waits; separate `lock=False` readonly proposal/item revision reads succeed with original query/transaction modes and see prewriter committed state. SQLite caller-owned BEGIN IMMEDIATE and savepoint remain usable; no helper commit. Do not claim these targeted cases establish cross-operation linearizability. |
| F1 canonical bytes and idempotence | Real refs unsorted, null/empty labels, Unicode; repeated digest at identical state (with changed actor) keeps original row/actor; independent literal canonical document/digest; varying description edit version, revise count, contract and refs changes appropriate components. Record actual canonical input strings with forwarding spies, actual stored SQLite text and PG JSON structures, exact row counts and no events from helpers. No expected value computed exclusively by production digest code. |
| F2 failure/ownership | Independent connection cannot see private freeze before owner commit; caller rollback removes it and preserves unrelated durable rows, caller commit exposes exact one row. Real scoped INSERT trigger failure leaves SQLite transaction usable/pending and PG INERROR before caller cleanup; then caller rollback restores original state. Python canonicalization failure before insertion leaves original caller pending state and no inserted row. Public reserve fault afterward retains accepted reserve atomicity/retry/no-idempotence behavior. |
| D1 branch/membership | No current and omitted input -> None; current and omitted -> exact digest; explicit same-item historical digest -> unchanged helper return, explicit foreign/missing/empty -> exact ReleaseMismatch; explicit must not invoke current read, default must not invoke membership read. PG cross-repo digest is refused even with matching item numeric ID. Public Decision sequence still admits/rejects at the original later constraints and does not append partial rows/effects on refusal. |
| C1 current query | None before freeze; revise invalidation; two Releases and re-reservation of older digest makes it current; referenced-vs-unreferenced order; multiple reservations on same digest; inactive/released reservation still affects ordering; old timestamps with higher IDs; equal timestamp ties; current revise_count exact. Independently seed legal provisional Release/reservation rows where public API cannot reach the no-reference fallback and tie cases. Assert zero writes and no extra lock/query. PG adversarial foreign-repo revise/reservation/Release rows do not affect own result. |
| P1 protected routes | Reuse actual reserve admission, keyed Decision, locked writer, Release roundtrip and native PG effect/evaluation/lease suites. Add direct hook-call identity/order assertions for aliases and normalization, readonly transaction cases and owner-pending success/failure where existing tests do not discriminate. No remote mocks offered as actual PG evidence. |

Before product extraction, demonstrate bounded negative controls against the frozen oracle: altered canonical digest/serialization; explicit->default fallthrough; wrong-item/tenant membership; omit revise-count filter; replace reservation-ID order with timestamp or newest Release row; commit inside participant; readonly basis unexpectedly locking. Prefer real persisted legal counterexample states and intentional isolated behavior mutations/forwarder controls; record each exact failed assertion. Do not mutate a protected coordinator oracle to make a candidate pass. Controls that only change mocked values cannot establish database/locking behavior. The minimal actual direct tests must distinguish these errors without broad new runtime hooks.

## Build and verification gates

1. Fresh native duplicate/dependency/refs/reservation read, governed child scope/Release and independent brief review. The supplied 298-item snapshot contains #2150 active, #2805 done and no separately titled open private Release-policy extraction; this is a bounded captured-state reconciliation, not a claim all semantics are absent. Refresh before creating a child. No item ID is invented here. Keep parent open.
2. Isolated source tree frozen at accepted main after ownership handoff; write/run/freeze own actual before oracle and falsifiers first, with coordinator ownership. If oracle discovers behavior outside this design or false parity, stop and shape separately rather than normalize it away.
3. Product scope only new private releasecore plus thin delegation in db/pg; releases.py existing value rules and protected callers remain unchanged. New tests/evidence allowed. No dependency/version/schema/API/IDL/served/deploy work. Preserve original query execution plans structurally; capture parameter/normalized token parity for both shared query shapes and actual result/fault/lock behavior.
4. Run fixed new oracle unchanged, both actual adapters; affected release/admission/reservation/keyed/locked writer suites, PG Release roundtrip + effects/readonly evidence + work leases/native reserve. Then full local nonperf suite and full disposable PG suite because both protected owner modules change. Check count, skips, source/helper identity, cleanup reports and exact node selection. Use separate frozen environments so syncing PG remote extras cannot strip served dependencies from local suite.
5. Source review independently checks exact before/after dynamic entrypoints, locks, normalization, query parameter order and eight writer callbacks/native invariants; verification-artifact validator, diff check and exact-head all four CI green before child acceptance. Retain per-case observations and chronology; no physical-crash/fsync, broad provider parity or parent acceptance inferred.

Proposed literal command families (new test files exist only after oracle authorship):

```bash
UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-release-policy-local-env UV_OFFLINE=1 uv sync --frozen --extra dev --extra served --extra remote
UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-release-policy-pg-env UV_OFFLINE=1 uv sync --frozen --extra dev --extra remote
UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-release-policy-local-env UV_OFFLINE=1 uv run --frozen --no-sync pytest -q --tb=short tests/test_release_policy.py tests/test_releases.py tests/test_item_release.py tests/test_release_integrity.py tests/test_reserve_admission.py tests/test_reservation_atomicity.py tests/test_keyed_decision_workflow.py tests/test_locked_decision_writing.py
PATH=/nix/store/lkcp4yn4dmkhmngwa363wwqyhhk5rzs4-postgresql-16.15/bin:$PATH UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-release-policy-pg-env UV_OFFLINE=1 timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh -k 'release_policy or releases or release_integrity or item_release or reserve_admission or reservation_atomicity or keyed_decision_workflow or locked_decision_writing or effect or evidence or lease'
UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-release-policy-local-env UV_OFFLINE=1 uv run --frozen --no-sync pytest tests --ignore=tests/pg --ignore=tests/test_work_application_pg.py -q -m 'not perf'
PATH=/nix/store/lkcp4yn4dmkhmngwa363wwqyhhk5rzs4-postgresql-16.15/bin:$PATH UV_PROJECT_ENVIRONMENT=/tmp/sprintctl-release-policy-pg-env UV_OFFLINE=1 timeout --foreground -k 30s 900s scripts/pg-disposable-tests.sh
python /projects/dev/agentops/scripts/validate_verification_artifacts.py --root .
git diff --check
```

Use manifest-required command families; final oracle collection must pin literal node IDs/counts after authoring, and verify the `-k` targeted selection actually includes every planned protected case (selection text alone is not a coverage claim). No baseline campaign is run merely to package this brief. Prior full suites remain attributed to their actual source; new source justifies the postchange gates.

## Parent completion accounting

After this child, basis/freeze/default-membership orchestration plus basis-count/current-selection query shapes can have per-family sharing+coverage evidence. `add_dep` remains an explicit bounded follow-up; backend-peer imports require explicit ownership disposition; public/private residual inventory must be refreshed by domain role, not same-name count. Original fixture-first historical requirement is separately investigated in the immutable history audit: presentation-param fixtures precede first extraction, actual shared mutation oracle chronology is not inferred. Parent #2150 cannot close because this Release child is green alone, nor does this brief revive superseded publicRepository/IDL requirements.

## Primary execution refinements before registration

The original optimistic brief is retained byte-for-byte in the pre-primary copy.
Use separate frozen/offline environments, --no-sync local execution, an explicit
local-only full gate and the pinned already-available PG16.15 PATH above. Record
literal stdout/XML destinations and actual collected IDs before product edits;
case counts are results, never promised invented coverage.

Basis missing-row discrimination remains `is None`, not a falsey mapping check;
current-Release decoder retains its original truthiness behavior. Dynamic backend
normalization/description lookups occur at the original point and no copied
normalized request/actor/item fields are introduced.

PG item IDs are globally unique. Foreign membership probes supply a legitimate
foreign item's numeric ID/digest under the wrong current repository; do not
fabricate two same-ID rows across repositories. Cross-tenant adversarial rows
must satisfy existing schema/domain relationships. Where an inner correlation
predicate is redundant under those valid relationships, keep exact structural
SQL token/parameter qualification without claiming an impossible legal-state
behavior discriminator. Direct provisional rows used for ordering must retain
valid digest/basis/item relationships; clearly label deliberate test-owned raw
fixture construction versus states reachable through public reservation.

Held-owner lock tests use finite deadlines, independent actual connections,
exact blocking PID confirmation and cleanup in finally. Prefer an unchanged
item held FOR UPDATE to separate locking from concurrent revision semantics.
Readonly controls must fail with bounded timeout/refusal, never hang a suite;
record native failure state before test-owner cleanup. This adds no product
statement timeout, new lock, cross-operation snapshot or fairness promise.

## Exact insertion method binding refinement

The pre-supplier plan is retained unchanged, SHA62483f0f9948759ae34da5e619a2d0c49172380e9307dd2706991c732ddb56d1.
Primary actual original SQLite helper characterization with an owned temporary DB
observed ref SELECT through the original bound method, contract serialization
rebinding the transparent proxy, and INSERT still through the original bound
method. Script62f7b0e96eead057b936cec941c680568e351220b45d172486cb62f319a20945
and raw trace remain /tmp/release-freeze-execute-lookup-characterization.{py,json}.
This is a deliberate test-owned lookup observation, not a new arbitrary-proxy API.

Independent review withdrew its earlier eager-insert-interface CLEAR and agrees
that the value-supplier seam above is required under the declared lookup boundary.
The oracle must distinguish digest-document canonicalization BEFORE execute lookup
from contract/refs serialization AFTER lookup. A digest-document wrapper that
changes the method must affect the selected insertion method; a contract/refs
wrapper changing it afterward must not affect that invocation. Actual SQL still
executes on the original owner, with supplier consumed once. An execute getter
fault prevents insertion contract/refs serialization; serializer faults prevent
actual INSERT and retain caller-owned pending writes. Final tuples or digest
equality alone are insufficient discriminators. Add actual positive before cases
and a pre-serialization/relookup negative control before product extraction.
