# Abrupt local process recovery verification

Verification-only child `sprintctl#2791` of unfinished `sprintctl#1131`, under
Release `a21c24e082f37d24624334530f8a21206f7830d8173a30d73267a938aa520240`, native No doc5754 and harness freeze5756.
The final accepted source is `d27985bc872f0fe8efcead9c25befef139ed623d`. Production code is unchanged.

Eight cases create an owned Python child with its own temporary SQLite connection,
pause at an observed application boundary, kill only that child with SIGKILL, and
reap its actual exit -9. The first fresh normal raw SQLite observer runs before
any product helper reopens the database. Pre-COMMIT and mid-batch cases recover
exactly the committed seed. After-return cases recover the whole completed
operation. Retries preserve producer event identity/sequence, complete cache
manifests and separate ingest/authority cursors. Authority offsets are sparse.

Five separate controls deliberately persist invalid sequence/cursor metadata,
persist an internally consistent partial new batch, or fail the trace guard.
The same raw checker rejects the stored-state violations. Guard failure exits73
without returning to SQLite, whose trace callbacks can otherwise swallow errors.
The fixed manifests provide expected-state assertions; structural self-comparison
checks alone are not independent proof.

The initial campaign passed 74 tests, including all 13 new cases. Independent
reviewers reproduced 13 cases each. The final accepted-source campaign passed
74 tests with no skips, errors or failures; exact commands and hashes are in
`verification/results/abrupt-process-recovery-2791.json`. Actual child import
paths, Python/SQLite versions, journal/synchronous settings, pending rows,
return/exit observations and recovered state are retained separately for the
initial and final captures. Measured modes include delete/2, wal/2.

These are application transaction boundaries, not observed fsync boundaries or
termination during COMMIT. Normal raw opening permits SQLite automatic recovery;
it does not establish which journal recovery path ran. No power/storage fault,
forced dirty-page spill, network outage, remote authority, trusted snapshot
installer, formal refinement or full parent qualification is established.
Final packet review and exact-head CI remain publication gates.
