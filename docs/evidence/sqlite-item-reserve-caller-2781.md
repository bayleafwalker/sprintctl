# Preserve caller work on refused reservation and item-status mutations

Bounded child `sprintctl#2781` of unfinished repository refactor `sprintctl#2150`.
Native decision #5602 freezes the reviewed scope under Release
`d28e692fdca133579c21836dfc2ce3a49593285c73a49136061d6c6ebbe9fca4`.

Actual temporary SQLite examples showed reserve admission and item-status
updates rolling back unrelated caller work when their nested BEGIN failed.
Both public wrappers now refuse active caller transactions before entering
owned rollback handlers. Reserve retains role, basis, contract and existing-item
checks before refusal; its clock remains afterward. Item status retains its
revision syntax check before refusal. Valid idle mutation paths are unchanged.
The refusal deliberately changes OperationalError with lost caller work to
ValueError requiring an idle connection with caller state preserved.

The private idle predicate is shared with the previously accepted sprint
mutation guards. Their wrapper name, exact message and call positions remain.
No transaction scaffold or new public interface is introduced.

The coordinator-owned oracle ran before product changes: eight caller cases
failed and five validation-order cases passed. After repair all 13 pass, along
with all 15 existing sprint caller cases. It retains seven complete table
snapshots, including existing reservations, Releases and Decisions, independent
durable reads, nested savepoint rollback/release and caller-created uncommitted
targets. The initial test-only basis/revision wording expectation was corrected
before the retained pre-repair run. Independent review found no issues and
passed 38 relevant cases. Affected regression suites passed 607 tests.

The context/result packets and actual before snapshots/output are retained in
`verification/`. The full local suite passed 2,739 tests (4 skips and 14 performance cases
deselected) in 588.41s. Exact-head canonical CI remains an acceptance gate.
PostgreSQL, keyed Decision caller behavior, broader private workflow extraction,
schema/API/IDL, deployment and parent completion are outside this repair.
