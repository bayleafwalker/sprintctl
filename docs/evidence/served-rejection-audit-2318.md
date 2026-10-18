# Served rejection-error reconciliation: sprintctl#2318

Date: 2026-10-18. Inspected source:
`3d194a784657c2ed0834db0cd2f08cb7880896dc`.

The pending item originally reported an unhandled `InvocationRejectedError`
from reservation admission. Its August note identifies the reservation repair;
its September note retains a remaining audit of served call sites after the
authority-reconcile repair. Both fixes and their regressions are present in
this inspected source. This reconciliation records the remaining source audit;
it introduces no product repair, exception policy, authority or deployment change.

## Call-site coverage

A Python AST traversal of every `sprintctl/commands/*.py` file enumerated all
`_served` attribute references and direct calls. An import/alias search checked
for separate `served` imports or dynamic `_served` lookups. The inventory has
49 facade attribute references and eight direct calls; a reference passed to a
wrapper is not counted as a direct transport invocation.

| Command module | Facade references / direct calls | Inspected rejection boundary |
| --- | --- | --- |
| `reservation.py` | 2 / 0 | `_served_result` sends identity resolution and reservation RPC through `_run_served`; both become a CLI error and exit 1. |
| `operations.py` | 10 / 5 | Normal calls, including reconcile records/decisions and batch apply, use `_run_served`. Two direct lambdas are optional release-trailer catalog/identity checks; `release_trailers.harvest_release_trailers` catches their exceptions and reports skipped harvest. Three direct calls construct native intake invokers, without invoking their RPC closures; reserve/proposal/evidence synchronizers catch typed owner rejections and retain their existing durable outcome classifications. |
| `inbox.py` | 1 / 1 | `_call` catches `InvocationRejectedError`, prints its typed JSON error and exits 1. Other exception types are outside that rejection-specific catch. |
| `session.py` | 8 / 1 | Ordinary reads use `_run_served`; direct `handoff_record` has an explicit catch that reports unconfirmed recording and exits 1. |
| `work.py` | 28 / 1 | Normal calls use `_run_served`; direct lifecycle arbitration has its existing exception boundary that preserves the durable request and reports failure. |

The common `_run_served` in `sprintctl/cli_runtime.py` catches exceptions,
prints a nonempty failure detail and exits 1. It does not turn rejection into
acceptance or retry an unknown write blindly.

Two adjacent non-command adapters were also inspected: `doctor.py` catches its
two direct catalog/identity calls into structured diagnostics; `volatile_hook.py`
supplies two delayed projection/validation calls to its adapter, whose handling
and outer entrypoint catch unavailable checks without authorizing a mutation.
These are distinct existing error policies, not an assertion that every failure
must use one formatting convention.

## Executed regressions

The independent reviewer ran the following selection at the inspected source
(**8 passed in 0.47 s**). The coordinating session reran it in an isolated
checkout with frozen dev/served/remote dependencies on Python 3.13:
**8 passed in 0.71 s**.

```sh
uv run --frozen --extra dev --extra served --extra remote pytest -q \
  tests/test_reservations.py::test_served_reservation_rejection_reads_like_every_other_served_failure \
  tests/test_served_authority_sync.py::test_served_authority_reconcile_rejection_reaches_the_operator_as_a_cli_error \
  tests/test_served_authority_sync.py::test_served_authority_reconcile_decision_rejection_is_a_cli_error \
  tests/test_served_failure_detail.py \
  tests/test_agent_inbox_cli.py::test_a_refusal_prints_the_error_code_as_json \
  tests/test_effect_explanation_cli.py::test_owner_authority_refusal_remains_explicit
```

The tests exercise explicit synthetic refusals through existing boundaries;
no test oracle, fixture or runtime code was changed for this reconciliation.
The static audit covers the remaining call-site requirement. Eight examples
are not exhaustive fault injection for every call, exception or future change.

## Negative-probe limit and disposition

Do not replay the original live `reservation reserve --actor <not-me>` example
as an expected rejection. Current reservation code resolves the authenticated
actor and ignores that override, so admission can succeed and create a real
reservation. Neither review ran that probe or used a live service write to
manufacture failure evidence.

Accept this backlog item as already implemented and now audited at the pinned
source, after the evidence change passes review and CI. That acceptance closes
the original CLI rejection scope, not all transport errors, native-intake
recovery qualification or deployment verification. The native item retains
immutable Git, evidence-digest and CI references.
