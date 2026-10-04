# S4 offline intake gate: isolated missing-capability preparation

Status: proposed verification preparation for agentops#2485, 2026-10-04.
This is not the full absorb gate, authority approval, migration, hook change,
import, soak start or TS-13 retirement. It changes tests and this plan only.

The governing integration direction remains agentops TS-6/Decision199 and
[the merged preparation note](https://github.com/bayleafwalker/agentops/blob/49ac361481d185a2b0c3e18899d19756096473d1/docs/plans/2026-10-03-s4-evidence-home-preparation.md).
Sprintctl's current implementation is pinned to
`f6936f410f41a7dfaf7e5a1390c552eb90954874` (0.12.0).
The active generic resource-authority design does not supply S4 intake.

## What the executable fixture establishes

`tests/test_s4_absorb_intake_gaps.py` runs actual reservation CLI, closed
outbox-contract construction, trailer harvest and served-sync orchestration
against temporary producer SQLite/Git state and a synthetic in-process
fake authority. No production profile, credential, network, live database or
source NDJSON is accessed. Existing test helpers supply a fake `.example`
profile; identity and served batch transport are replaced before invocation.

It exercises six bounded, example-tested histories:

- An unavailable authenticated identity causes served reserve to fail before
  any local reservation fallback or durable pending outbox request.
- Each native operation `work.reservation.reserve`,
  `work.evidence.append-v1` and `work.effect.propose-v1` is refused when
  relabelled as an outbox AuthorityCommand; no request is appended.
- The isolated offline sequence attempts reserve, commits a harmless exact
  synthetic `Vuoro-Release` trailer, attempts the missing carriers and retains
  an ordinary authored observation. Restart/sync can upload the observation
  and trailer, including repeated sync and commit-before-reply-loss replay,
  without creating an effective reservation, run-bound evidence or proposal.

The fake only records observation receipt plumbing. It does **not** implement
Sprintctl domain acceptance, authenticated authorization, per-run tail locks,
effect decisions, expiry or an evidence chain. Duplicate observation replay in
this fake is not operational evidence for those owner protocols. A successful
sync, zero `pending_command_event_ids` and zero unsupported command ids cannot
establish that missing requests were durably captured: none existed to report.
The fixture therefore records the S4 gate as **unproven/failing prerequisites**,
not a passing absorption test. The source matrix must not be broadened merely
to make these tests green.

Reproduce using only isolated test fixtures:

```sh
uv run --extra dev --extra served pytest -q \
  tests/test_s4_absorb_intake_gaps.py tests/test_served_authority_sync.py
```

## Minimum owner work before a real gate

1. Specify durable producer requests for reserve, run-bound evidence append and
   effect proposal through the existing outbox/sync owner. The request format
   must bind repository/workspace/principal, operation identity, original
   payload digest, idempotency key, exact Release/item revision and dependency
   order. Do not place tokens/assertions or reusable authority in local rows.
2. Preserve advisory reservation semantics. A pending reservation request is
   neither a shared reservation nor an execution lease. The current reservation
   INSERT has no general replay contract; its owner must specify idempotent
   commit/reply-loss behavior before adding a durable carrier. A revision
   predicate cannot be fabricated where today's operation has none.
3. Evidence append needs a real registered run, principal/workspace binding,
   stable request digest and expected-tail reference. Offline observations are
   not that receipt; no synthetic run or tail may be substituted. The owner
   chooses safe refresh/reconciliation after concurrent append or stale tail.
4. Proposals must keep immutable intent id/revision/digest, evidence and Release
   references and authenticated capability checks. Offline capture conveys no
   grant, acceptance or invocation permission. Acceptance remains with the
   explicit owner route and historical ledger.
5. Define receipt correlation and dependency replay after process restart,
   unknown commit outcome, stale identity/Release, changed inputs and expiry.
   Request identity survives retries; source content conflicts stop the stream.
   Unsupported/blocked requests remain visible and durable rather than being
   silently discarded or mistaken for effective state.
6. Then run the actual isolated pre-merge gate with a disposable **real** owner
   authority/database and controlled evaluation clock. Compare every canonical
   effective field against the continuously online reference history in the
   same causal order. Include CAS conflicts, concurrent append, commit-before-
   reply-loss, expiry and accepted+used+dead or missing-use-receipt histories.
   Do not whitelist authority/expiry fields as transport metadata.

The current fixture intentionally does not design new schema grants, activate
legacy import, alter producer hooks, substitute resource aggregates, change
runtime carriers or rewrite the accepted Decision. Those changes need their
separate owner contract, review and disposable integration proof. Until the
real gate, digest-preserving import and coordinated cutover succeed, auditctl
stays separate, both append-only guards remain, and soak dates stay unset.
