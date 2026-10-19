# Producer restart through actual synchronization

Verification-only child `sprintctl#2810` strengthens ADR F02 under unfinished
`sprintctl#1131`. Native freeze5857 binds Release
`01b2473fd9cb1e7624e7333977cecb1fa9ab553d7ec4e189e1bca2f4f24d892b`.
The qualified accepted source is `32bc4e6b3b1fa1c369fcd139605d0dff6795488e`.
Production code is unchanged.

Two histories join the previously separate local recovery and remote ingest
proofs. An owned producer appends committed records, reports their exact returned
identities and idle owner state, then is killed and reaped with SIGKILL at that
application-return boundary. A separate raw observer opens SQLite normally
before any product helper reopens it. A fresh producer process calls the actual
public synchronizer against disposable PostgreSQL and a temporary cache. Another
fresh producer replays it, preserving original offsets and values, then extends
the observational stream with sequence4. That last upload reports three duplicate
records and one fresh record.

The second case retains an unsigned LOCAL authority request followed by an
observation. The request names a real committed fixture item. With no credential
resolver, the prefix remains pending and every independently read authority table
stays identical. A local request or cached observation does not authorize a
transition or acceptance.

Independent known answers cover all authored fields and canonical payload text,
local and remote digest domains, returned records/cursors, exact retained generated
fields, complete cache contents and separate authority watermarks. Six separately
owned raw observers per history verify predeath, first recovery and postworker
outbox/cache state. Their marker reader, copy helper, output bounds and kill/reap
paths have finite test-owned deadlines. Workers spawn no metadata subprocesses.

The frozen campaign passed46 local cases and35 actual PostgreSQL cases, with
seven registered fixture cleanups empty. Its18 new observations comprise two
joined histories, seven persisted-state controls, four real bad-checkpoint
controls and five reached-stall controls exercising the same marker deadline
reader. Control cleanup kills are distinguished from the two qualified producer
crashes. The extractor reproduces the published trace object from actual JUnit.

Initial development exposed two incorrect test assumptions: producer generation
truncates to whole seconds, and the local fingerprint covers fewer fields than
the remote full-envelope digest. Those failed captures remain historical.
Independent review also strengthened fourth-row provenance, returned API checks,
postexit durable readback and deadline controls before the final harness freeze.

The original preauthor source and30/33 existing-test baselines belong to1ec78fa.
The accepted-source refresh to32bc retains14 of15 pinned files exactly; only the
accepted Release helpers/import changed in PG, with all other named PG ASTs
identical. Rebase-stage oracle hashes precede the final returned/runtime assertions.
The copied final freeze pins the four files actually qualified. No earlier run is
relabeled as this final campaign.

PG isolation was not queried; it remains unknown. The independent observer ends
its transaction between committed phases. This evidence does not claim a shared
snapshot, exhaustive schedule coverage, network outage, physical fsync/power loss,
whole-business shadow equivalence or full parent qualification. F01, F13, F14 and
formal refinement retain separate obligations. Exact-head CI, independent final
packet review and merge equality remain publication gates at packet creation.

See the [context](../../verification/contexts/producer-restart-sync.json),
[result](../../verification/results/producer-restart-sync-2810.json),
[frozen harness](../../verification/fixtures/producer-restart-sync-harness-freeze-2810.json)
and [actual observations](../../verification/traces/producer-restart-sync-2810.json).
