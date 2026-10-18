# Shared locked keyed Decision workflow

Bounded child `sprintctl#2789` of unfinished `sprintctl#2150`, governed by native
No doc5697 and oracle freeze5707 under Release
`3e5a3d56bb0338a7aaf960ab0e0f1cfe77661af15d1737b6901926407d6d233b`.

Private workitemcore participants now share locked target lookup, keyed replay,
current revision comparison, legacy re-mark detection and existing Decision call.
Backend wrappers keep normalization/key/basis validation, exact SQL/key locks,
transaction ownership and native rows. PostgreSQL replay rollback stays inside
its item cursor; fresh canonical event insertion and commit follow cursor exit.
The helper neither reconstructs returned Decisions nor owns events or transactions.

Before product edits, the independently reviewed oracle passed 311 local tests
and 487 actual PostgreSQL tests with all 14 cleanup checks empty. Forty-six
shared examples per backend and 23 PG-specific cases cover replay mismatches,
omitted Release asymmetry, terminal/legacy state, caller effects and native
report/lease consequences. Two test-only review findings strengthened actual
wait observation and replay-object identity before the final freeze. The three
oracle files remain unchanged through extraction. The initial 408 adjacent function/class
ASTs and decisions.py bytes remained unchanged; only the two keyed wrapper bodies
change alongside the new private core symbols.

Four bounded held-writer schedules observe a contender waiting before commit:
SQLite records attempted BEGIN; PostgreSQL checks the exact holder blocking PID.
They establish the tested key invariant, not global linearizability or fairness.
After-real-Decision/payload/event faults observe actual pending owner state and
independent durable state before cleanup, then same-key retry succeeds. Discarded
committed responses are retried on a new connection; no physical crash is simulated.
PostgreSQL lease/report extension timestamps serialize as strings; shared snapshots
include native type metadata. Neither representation authenticates external origin.

Existing caller policy remains explicit: both wrappers join implicit caller
transactions; fresh requests commit caller writes and replays roll them back.
Pretry validation leaves pending caller writes. PG managed/two-phase failures can
leave effects pending until caller cleanup. This refactor preserves that behavior
and does not add a safe-composition guarantee or invent verifier evidence.

After extraction the frozen focused oracle passed 46 local and 487 PG tests;
all 14 PG cleanup checks were empty. After accepted reservation-admission and docs
work advanced the base to `4718d49467234e9362fe1ec6430da77456b7dca8`, all five
owned candidate ASTs and three frozen oracle files stayed unchanged; all 410
adjacent top-level symbols match that final base.

The corrected final-source full local suite passed **2,959 tests**, with 4 skips
and 14 performance exclusions in 695.64 seconds. Combined final-source PostgreSQL
regressions passed **551 tests** in 19.68 seconds, with all 16 cleanup instances
empty. The earlier full PostgreSQL run passed 1,586 tests with 85 empty cleanup
instances on the initial base; it remains historical evidence for that source.
It is not relabeled as a final-base full run.

The first full local run failed one harness test because uv could not write its
sandboxed cache. Its log/XML and exact-case diagnostic remain recorded separately.
The unchanged case passed with the normal writable cache, then the corrected full
suite above passed without a product or test change for that harness failure.

The [result packet](../../verification/results/locked-keyed-decision-2789.json)
records commands, raw hashes and declared source provenance. Separate before and
after captures each retain 18 actual observations. The final after capture comes
from final-source full local and combined PG JUnit: 14 sequential observations and
4 held-writer schedules. Packet review and exact-head canonical CI remain
publication gates. No schema,
API/IDL, authority, reservation-admission, deployment or parent closure is included.
