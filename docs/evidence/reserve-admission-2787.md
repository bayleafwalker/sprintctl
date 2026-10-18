# Shared reservation admission effects

Bounded child `sprintctl#2787` of unfinished repository refactor `sprintctl#2150`.
Native No doc5674, oracle freeze5704 and Release `5168d9d2d0e5f56fd134331bdb4a3a38ccf7e4b976ae1a4b015917ce3b832b63`
govern this source change. The base is `7b221ea3732bffbce03d0a7fab797f8939bf6872`.

The SQL-free private workflow shares Release/basis checks, ordered overlap and
takeover, reservation creation, lifecycle events and result annotation. Backend
participants retain fixed SQL, tenant predicates and native row types. SQLite
retains validation, public existence lookup, idle guard, captured clock, BEGIN
and database-time maintenance check. PostgreSQL retains same-connection refusal,
validation, captured clock, repository advisory lock, maintenance check and item
basis lock for every role. SQLite still resolves a basis only when needed and
re-reads the event item after insertion; PostgreSQL uses its already locked item.

Admission is distinct from the lifecycle driver's transaction convention. Its
shared participant never commits or rolls back. The public wrappers and hosted
native replay ledger retain their original owners. Matching native replay skips
new admission; repeated unkeyed calls remain new advisory rows. Existing caller
and unknown-commit behavior is preserved, without a new composition guarantee.

Before product edits the paired oracle ran 30 shared cases per backend plus 6 PG
cases. The broader baseline passed 141 local tests and 304 PG tests, with 10 empty
cleanup checks. Independent oracle review reproduced 30 SQLite cases; its primary
PG isolation-measurement advisory was applied before the final baseline. Tests
cover exact request/event values, raw timestamp types, input nonmutation, stale
basis, takeover order, actual INSERT/UPDATE faults, result/event exceptions,
caller-owned provisional effects and captured-clock waits. Existing atomicity,
Release, maintenance and native replay/commit-loss tests ran unchanged.

After extraction the same selection passed 141 local and 304 PG tests. The full local
gate passed 2913 tests with 4 skips and 14 performance cases
deselected; full PG passed 1553 tests with 85 registered cleanup
instances empty. Nineteen retained before observations remain separate from
nineteen full-run observations, each with 17 sequential examples and 2 bounded
actual lock-wait histories. They establish the tested schedules, not arbitrary
isolation, fairness, physical crash safety or runtime identity qualification.

Source review is clear. Exact-head canonical CI and final packet review remain
publication gates. No provider/API/schema/IDL, deployment, field qualification or
parent completion is claimed. See `verification/results/reserve-admission-2787.json`
for hashes, precise bounds and execution provenance.

The final publication base is `479ce6eb8afc76c890110b5c7fa03a47243567f4` after README-only PR182.
Owned product bytes are unchanged from the full-tested base above; combined
documentation/install checks passed 18 tests. Exact-head CI qualifies this final
combined tree.
