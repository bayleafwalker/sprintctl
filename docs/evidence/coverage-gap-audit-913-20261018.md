# Test coverage gap audit: sprintctl#913

This delivers the audit requested by the item's title: state transitions,
claim conflicts and maintenance edge cases. It identifies tested examples and
remaining questions; it does not claim every gap is fixed or complete protocol
qualification. No product, schema, authority or deployment behavior changes.

## Baselines and reused evidence

The shared [14-failure/9-invariant matrix](protocol-coverage-audit-20261018.json)
is retained **unchanged** from the separate #1131 reader's observation at
`072d0338c77848252d4986a0e6e15b9432a2a085`. Its SHA-256 is
`fe8596395c7d217ee34094ae292b7050258730ec54fc98786209a6451056f5b5`.
It records 23 requirements, 70 resolved source/test anchors and 11 stale
anchors among 38 legacy context declarations in four of seven contexts.
Those are findings from that observation, not current-runtime pass counts.

This audit re-resolved all 70 anchors successfully at source `4a00d182` before
the separately owned reservation-atomicity fix `20e120f` landed. The local
verification baseline then advanced to include that repair. The original
matrix is preserved rather than rewritten to impersonate a newer observation.
Its 90 local passes, unexecuted PostgreSQL histories and unexecuted formal
checker remain the original reader's evidence, separate from this audit.
Accepting #913's audit does not accept #1131's larger qualification requirement.

## Findings by requested area

| Area | Current evidence | Remaining limit or gap | Current owner |
|---|---|---|---|
| Item and sprint transitions | `tests/test_core.py::TestStatusTransitions`, `tests/test_decisions.py`, and stale-command examples in `tests/test_authority_fault_protocol.py` cover accepted/refused transitions, expected-state CAS, terminal Decision binding and no second stale-command effect. | Examples are not an exhaustive generated state machine. State-based revisions are not a monotonic fencing epoch; no general ABA or external-effect fencing proof follows. | Sprintctl state/Decision owner; #1131 F09/I05/I06 retains the wider qualification limits. |
| Historical claim conflicts | #1131 F08/I07 correctly distinguishes the retired credential-bearing claim protocol from current advisory reservations and the separate protected owner lease. `tests/test_reservations.py` covers overlapping reservations, explicit interruption, session attribution, reassignment, release and marker/scope guards. | A reservation is neither a lease nor mutation authority. Restoring exclusive claim/TTL tests would assert the wrong contract. Hosted owner-lease partition/expiry histories remain distinct PostgreSQL evidence. | Sprintctl reservation owner and protected work-lease owner; #1131 F08/I07. |
| Reservation failure histories | The separately landed #2763 repair (`20e120f`) adds shared rollback fault cases in `tests/_reservation_atomicity_contract.py` and local/PG runners. This audit includes its local runner. | This is evidence for the listed reservation operations, not a whole-maintenance-command transaction or distributed fencing proof. | #2763 repair owner; historical full qualification remains separate. |
| Maintenance diagnostics | `tests/test_maintain.py::TestMaintainCheck` covers stale/unknown/empty state, policy thresholds, reason-coded findings and read-only behavior. `tests/test_served_maintain_check.py` exercises the real owner through the CLI facade and current failure handling. | Local/facade examples do not qualify every production repository, runtime or live migration condition. A diagnostic is an observation, not admission to perform maintenance. | Sprintctl maintenance owner; existing served route delivered under #2305. |
| Sweep and carryover examples | `tests/test_maintain.py` covers fresh/stale items, repeated sweep, stale reservation interruption, auto-close with/without active work, carryover source/target errors, same-sprint refusal, empty/done filtering, track preservation and both-sprint events. | No injected crash/commit failure campaign for the entire multi-step sweep/carryover was found in the inspected maintenance test modules. Whole-command atomicity, retry after a partial command and competing sweeps are not established by these examples. | Sprintctl maintenance owner; a separate contract/verification decision is needed before any repair. |
| Served maintenance availability | The route registry and CLI classify `maintain check` as an existing owner operation; `maintain sweep` and `maintain carryover` remain explicitly unavailable in served mode. | Local command tests are not evidence that a served mutation endpoint exists. Do not add an automatic direct-PostgreSQL fallback. | Sprintctl CLI/maintenance owner; existing availability inventory in [served coordination](../guides/served-coordination.md). |

## Follow-up boundaries

The following work remains open rather than silently satisfied by this audit:

1. #1131: actual fsync/process-crash boundaries (F01), trusted snapshot recovery
   beyond safe suffix refusal (F14), current applicability of historical model
   requirements, and a refinement mapping if the legacy proposal model remains
   required. Source-present tests and a bounded old model do not prove that map.
2. #1131's stale context anchors: reconcile names and current semantics while
   preserving historical result packets. Anchor drift does not automatically
   imply missing implementation.
3. Sprintctl maintenance: specify the intended failure/retry boundary of
   multi-step sweep/carryover, then verify it with temporary stores, injected
   failures and independent actors as appropriate. No new atomicity promise or
   production-semantic repair is authorized by this audit.
4. Keep hosted lease qualification separate from advisory reservation evidence.
   The owner-lease tests were inspected, not rerun by this audit, and external
   effect fencing remains outside these local examples.

## Executed verification

At `4a00d182a2ac96a844c2518ac1e7823e7f53d321`, the selected local transition,
Decision, authority-fault, reservation, maintenance and served-diagnostic suite
passed **135 tests in 13.46 seconds**. All used temporary repositories and
SQLite fixtures, with fake served transport where declared. No PostgreSQL or
formal checker ran for that coverage audit.

After the #2763 reservation repair landed, the current local cohort additionally
included its atomicity runner and the separately relevant recovery/doctor
checks. The exact final source, command and result are retained in
`verification/results/coverage-gap-audit-913.json`. Recovery verification
belongs to #1234 and must not be counted as evidence for unexecuted hosted
lease, maintenance or #1131 fault histories.

The audit has no live fault injection, secret/credential inspection, migration,
deployment or authority change. Every claimed result is bounded to the cited
examples; the open questions above are part of the deliverable.
