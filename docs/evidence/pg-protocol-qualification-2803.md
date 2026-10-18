# Current PostgreSQL protocol evidence

`sprintctl#2803` supplements unfinished `sprintctl#1131`. It replaces a source-only
PostgreSQL qualification gap in the historical 23-row audit with a fresh, bounded
campaign on accepted source `d27985bc872f0fe8efcead9c25befef139ed623d`.
The original audit and all prior verification packets remain unchanged.

Exactly **26 selected cases passed** in 3.07 seconds, with 1,596 cases deselected.
All seven registered fixture cleanup checks found zero remaining rows. The actual
JUnit node set equals all 26 planned invocations from 21 logical test anchors:
no missing, extra, failed or skipped cases. Tests use disposable PostgreSQL 16.15
and temporary SQLite producer/cache files.

The [result](../../verification/results/current-pg-protocol-evidence-2803.json)
links the [exact case reconciliation](../../verification/fixtures/pg-protocol-reconciliation-2803.json),
[raw JUnit](../../verification/traces/pg-protocol-junit-2803.xml),
[stdout](../../verification/traces/pg-protocol-stdout-2803.txt) and
[runtime identity](../../verification/fixtures/pg-protocol-runtime-2803.json).
An observation-only plugin checked the accepted HEAD, tracked source status and
nine actual imported module paths and hashes before and after the campaign.
It recorded Python 3.13.16, pytest 9.0.2, psycopg 3.3.3, and READ COMMITTED on one
independent metadata connection. This does not newly measure every test actor's
isolation or establish an external chain of source authenticity.

The comparison retains 102 historical file hashes. All 21 selected test function
source segments and full test files are unchanged. Three product files differ
through the separately accepted reservation-admission extraction #2787; its
reviewed participant preserves backend transaction and native effect ownership.
Earlier full PostgreSQL evidence remains attributed to its original source.

The bounded assertions supplement 16 historical rows:

| Rows | Evidence established by the selected tests |
| --- | --- |
| F03, I02 | Original offsets on retry; one admitted record for two independent same-stream writers; changed producer timestamp conflicts. |
| F04 | Sequential retry after committed ingest retains the repository cursor. |
| F05, I03 | A missing sequence refuses the batch; a corrected contiguous batch is accepted. |
| F08, I07 | Advisory takeover refuses old-session touch; separate expired or superseded hosted leases retain reports while refusing settlement. |
| F09, I06 | Exact command replay retains identity; stale basis refuses mutation; successful sprint close retains its boundary and Decision. |
| F10 | Retired capability commands and acceptance without a required receipt are refused. |
| F11 | Continuation preserves context boundaries; a superseded old holder is informed on retry. |
| F12, I08 | Native proposal replay/content conflict, two independent producer reconciliation, and injected reply loss after actual owner commit in four proposal states. |
| F13, I09 | Actual PG-ingested envelopes applied to a fresh SQLite cache reach expected offsets and event IDs. |
| I05 | A real SQL Decision-insert trigger failure rolls back command admission and the item effect. |

Two existing tests run independent competing writer connections. They retain
start barriers and assertions, but emit no raw invocation/completion histories
or observed lock-wait trace. Concurrency labels are limited to those tests;
`execution.histories` is zero. Sequential examples remain example-tested.

Seven rows receive **no new PostgreSQL qualification**: F01, F02, F06, F07, F14,
I01 and I04. This campaign does not establish physical fsync or power-loss safety,
actual HTTP failure for F04, trusted snapshot installation, generic proposal-model
refinement, complete business-state shadow parity, or cross-provider qualification.
F13/I09 do not call the cache rebuild function or test schema upgrades. Separate
#2791 owns selected abrupt process tests. Neither child completes #1131.

This is a verification packet only. Product code, existing tests, public contracts,
service configuration and deployment state are unchanged. Exact commands and
source/fixture identities are retained with the packet; final artifact/docs checks
and independent review apply to these newly published files.

Publication follows the separate #2791 tests/evidence merge at
`cc316d1561ff20a298d316079ad308f32124d28a`. Its ten added files leave product
modules, existing selected tests, fixtures, runner and dependency metadata
unchanged. The campaign remains attributed to its actual `d27985bc…` source;
publication CI checks the resulting new tree separately.
