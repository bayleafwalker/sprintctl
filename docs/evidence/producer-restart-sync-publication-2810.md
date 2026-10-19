# Producer restart publication requalification

The first publication candidate collected PostgreSQL tests with an unconditional
`psycopg.sql` import. Canonical CI's local Python3.11/3.12 environments omit that
optional dependency, so run39297478354 failed during collection. Move the import
into the sole PG-only helper, `remote_state`, allowing the existing `PG_MARKS`
skip contract to apply. Product code and runtime assertions are unchanged.

The original qualified PG module is preserved byte-for-byte as
`verification/fixtures/producer-restart-sync-qualified-pg-2810.py`, SHA256
`29c9c3dbedf27e63dc774774d8954b7c3e2065437a485041987f47d6fd9de4a7`.
The historical result and harness freeze identify that original path/hash at
`32bc4e6`; they are not rewritten to describe the corrected source. The repaired
module SHA256 is `a43e68e700b078bcf5d4dbee50e329c7c0ba63436f82f208830caf08d09aaf89`.
Both identities are mapped in the separate publication freeze and receipt.

Fresh qualification on accepted base `974107f` passed46local and35actualPG tests,
with7registered cleanup scopes empty. A separate environment containing dev and
served extras, without remote, passed16local cases and skipped the2PG cases
through their existing markers. Those skips establish collection compatibility;
they are not counted as PostgreSQL qualification. Fresh raw logs, JUnit and18
extracted observations are retained separately from the historical campaign.

The independent review and all four canonical CI jobs must cover the corrected
final commit. Native acceptance binds its exact tested/source/merge tree and
original frozen Release. Process-kill proof remains bounded application evidence;
PG isolation is unknown, and parent1131, physical durability, network faults and
whole-business shadow parity remain separate.

- [Historical evidence](producer-restart-sync-2810.md)
- [Publication freeze](../../verification/fixtures/producer-restart-sync-publication-freeze-2810.json)
- [Publication receipt](../../verification/fixtures/producer-restart-sync-publication-2810.json)
- [Publication result](../../verification/results/producer-restart-sync-publication-2810.json)
