# Existing unmet-obligation report reuse

Bounded child sprintctl#2774 of unfinished repository-layer item#2150.
The frozen native scope is decision5569 and Release
`1be95c31ab1cb0e6890de462d309c4e9a761a2ac47f786d3f4d0035986d96950`. Source base is `0b1b18004fa968e9919eeeb924b038e6b18b9986`.
This private extraction preserves existing#2446 behavior; it does not define a
new public repository or provider contract.

The SQLite and PostgreSQL entrypoints repeated the same domain reduction:
load a Release, collect its declared evidence labels, read accepting Decisions,
and report every label for each Decision without any evidence digest. The list
entrypoint combines reports, sorts by item/Decision, counts all rows, and only
then converts the limit and applies Python slicing. That workflow now lives in
`obligationscore.py`. Private drivers retain existing SQL, JSON decoding,
repository/sprint selection and malformed-digest validation.

A syntactically valid absent digest returns an empty report; a malformed digest
still raises. Undeclared and empty obligations cause no Decision query. Any
evidence digest satisfies every label, because this report has no evidence-kind
matching rule. Declared order and duplicate labels are preserved. Accepts bound
to an older Release are assessed against that Release, rather than a newer
current Release. Aggregate counts precede slicing; default, zero, negative,
string-convertible and invalid limits keep existing behavior. The helper does
not add a transaction, commit, clock read or snapshot promise.

The shared actual-backend oracle was written and passed before product edits:
23 SQLite cases and23 disposable PostgreSQL16.15 cases. The21 shared cases cover
exact rows, absent/malformed/undeclared/met/unmet facts, non-accept exclusions,
limits, historical Release binding, sprint filtering and independent returned
lists. Additional SQLite checks compare row dumps and database-file bytes and
preserve a pending caller write. PostgreSQL checks preserve caller-write
visibility and exclude a separately registered tenant. The unchanged oracle
passed again with existing Release/obligation tests after extraction:77 local
and31 PostgreSQL cases. Both PostgreSQL runs confirmed zero remaining fixture
rows. The full local CI selection passed2667 tests, with4 skips and14 performance
cases deselected, in386.01s. The full disposable PostgreSQL suite passed1337
tests in81.18s; all79 cleanup instances across4 fixture names reported zero
remaining rows. Exact commands and tool versions are retained in the result
packet.

Independent optimistic review found no compatibility drift and confirmed the
same SQL predicates, materialization, early returns, error timing and dynamic
default lookup. The frozen oracle hashes remain unchanged. See the separate
context/result packets for exact bounds and candidate hashes. This is bounded
example evidence, with no new concurrent-reader snapshot or whole-provider
parity proof. No service deployment or schema/API/IDL change is included.

Static sprint and tenant exclusion are example-tested. Unchanged current-item
membership joins are separately source-inspected; no item relocation race or
concurrent snapshot was qualified. Returned-list checks establish freshness
across report calls, without manufacturing multiple terminal accepts on one
Release.
