# Fresh retirement rationale: sprintctl#2240

Date: 2026-10-18. Initial measured baseline:
`16f121f1fc527f3238953b6f203b32530cdfb5bc`. Final verification base:
`95f8fedf0a5023cba6fdf8cf668f8e3bbce122bb` plus the candidate file hashes in
`verification/results/retirement-rationale.json`.

The original request proposed a new rejected status to retire pending, blocked
or active work without asserting acceptance. The current TS-5 model already
represents that distinction through one terminal Decision and its rejected,
withdrawn or superseded resolution. This change implements the remaining
reason requirement within that existing model. Scope was recorded in native
decision note 5398 and pinned contract reference 1112; no new plan was ratified.

## Fresh admission and preserved history

New `reject`, `withdraw` and `supersede` decisions require at least one
non-whitespace rationale character. The authored string is retained unchanged.
This is an application admission rule, not a language-level assessment of the
reason's quality or a new database constraint.

The shared `decisions.decision_error` checks existing terminal/legacy rules
before the new fresh-admission condition. Both backend keyed writers perform
exact historical replay before calling it, and before comparing the original
status revision again. Historical empty and whitespace rationales remain
unchanged and replayable; different content under an existing key conflicts.
`accept`, its status-done alias, and `revise` retain their optional rationale.
Legacy re-mark rules still require their existing rationale and evidence.

Fresh-writer inventory:

- SQLite `record_decision_keyed` and status-done use `db._decide_locked`.
- PostgreSQL `record_decision_keyed`, status-done, and hosted settlement use
  `pg._decide_locked`.
- Native authority `decision.record` checks the shared admission rule and then
  calls the same PostgreSQL primitive. Its item-done path writes an acceptance.
- Carryover uses the ordinary owner writer with its existing authored rationale
  and superseding item reference.
- Each fresh item-decision primitive checks `decision_error`. The separate
  direct PostgreSQL decision insertion folds historical capability receipts;
  it is not fresh item admission and remains unchanged. Recovery/import also
  preserves historical rows rather than re-admitting them as new requests.

No status, lifecycle, API, catalog descriptor, DDL, grant, package pin or service
deployment changes. This does not repair dependency-readiness interpretation;
that remains separately owned by the dependency/dispatch work.

## Verification

Before the implementation, the first shared contract produced **27 failures
and 13 passes on each backend**: every null, empty or whitespace rationale
was admitted across the three retirement kinds and three open states. Both
runs used temporary sources; PostgreSQL ran in a private disposable 16.15
cluster through the repository's safety preflight.

The expanded shared contract covers 49 cases per backend, including six
historical replay cases retaining the now-stale original CAS revision, three
served-boundary refusals, and three independent-connection identical-key
histories. Each concurrent history commits one decision and one item-decided
event, returning one fresh result and one replay. SQLite additionally restores
two historical blank-rationale snapshots and replays their original requests.
Three PostgreSQL cases exercise native authority-command rejection directly.

Current results:

- **2,440 passed, 4 skipped, 1,189 deselected**: complete non-PostgreSQL,
  non-performance suite, 197.22 seconds with the cached-parser offline setting
  described below.
- **52 passed**: all SQLite rationale cases plus the remaining resolution
  rendering fixture, in the fresh frozen Python 3.13.16 environment.
- **1,175 passed**: complete disposable PostgreSQL 16.15 gate, 69.79 seconds;
  72 fixture cleanups across four fixture names, all with zero remaining rows.
- Required verification-artifact validation passes.

Existing release, lease, export and resolution tests now supply reasons for
their fresh retirement setup; their original assertions are retained. Historical
normalization tests still require empty rationale to normalize unchanged.

Earlier broad runs exposed stale copied-environment subprocess paths and the
already tracked DST issue on the old baseline. Final verification uses a fresh
`uv sync --frozen --extra dev --extra served --extra remote` environment and the
merged clock/DST fixes. No production state or shared artifact root was used
for test data.

The existing workflow-YAML check runs an auxiliary isolated `uv --with pyyaml`
parser. In a sandboxed broad run its package-metadata refresh failed at PyPI
DNS resolution even after cache warmup; the same check passes with
`UV_OFFLINE=1` against the prewarmed cache. The final local gate uses that
setting, without changing a product dependency, source or test. This verifies
the cached parser and project tests, not network installation availability.

The parent independently reviewed admission ordering, writer convergence,
fixture preconditions and the historical retry boundary before commit. The
evidence is bounded example/concurrency testing, not a proof of all possible
faults or raw SQL enforcement. Temporary environments and logs are session-local;
the committed protocol evidence becomes cross-host-replicated through Git.
