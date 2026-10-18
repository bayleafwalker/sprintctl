# Credential-free reservation criteria: sprintctl#1237

This is a bounded verification and criterion reconciliation. **#1237 stays
pending**, and its dependency relationships are preserved. It does not accept
the missing Vuoro twin, qualify a deployment or introduce a replacement API.

Sprintctl source: `e31124de724e4716ffbe71ca021394f742b3ec39`.
Vuoro source: `aa5af4de351f011c1d4f795eaa72b692c5dfcb47`.
The [reservation protocol](../protocols/reservation-model.md) supersedes the
historical v3 plan pointer and is referenced by repository guidance; its
frontmatter remains **draft**. This audit does not ratify it.

## Native criteria versus current owners

| Native requirement | Current evidence | Disposition or limit |
|---|---|---|
| Remove work claim bearer-proof fields and commands | All **107** current operation input/result schemas were recursively inspected: no `credential_ref`, `proposed_credential_ref`, `coordinate_credential_ref`, `claim_token` or `ownership_proof` schema keys; no `work.claim.*` operations. Current reservation handlers/read results are credential-free. | Credential-free means no issued work-claim proof. Authenticated transport and operation authority remain required; ordinary authentication credentials are not retired. |
| A retry returns one durable reserve effect | The separate `work.reservation.reserve-v1` owner uses its principal/workspace/repository key namespace and durable ledger. Real PG cases cover normalized replay, changed-request conflict, commit-response loss, same-key concurrent writers, immutable admission snapshots, maintenance interleavings and lifecycle-aware replay. | Its current result uses `replayed` and `admission_snapshot`, not the historical `duplicate=true` spelling. Replayed admission is not renewed execution authority. |
| Old direct reservation retry behavior | Both `work.reservation.reserve` and `reserve-v1` catalog descriptors require an envelope key. The legacy facade generates a new key per invocation; the legacy handler does not implement the native owner's durable deduplication. | Required-key metadata alone does not prove deduplication. The advisory CLI reserve remains one-shot; observe current state after an unknown result. No silent migration to the native owner occurs. |
| Catalog discovery/version and parity | Current data-only descriptors, route bindings and compatibility tests are present; native reserve is a separately closed addition with prior descriptors preserved. Vuoro composition names `work-api/v1` and `work-schema/v1`; the wire catalog is `operation-catalog/v1`. | These names are different contracts from the old item wording “catalog v2”. Do not downgrade/relabel a current catalog or assert the old literal criterion is fulfilled without owner reconciliation. No fresh deployed catalog was queried. |
| Vuoro Q5 transient carrier retirement | Current Vuoro `InvocationRequest` is strict `invocation/v1`; source tests reject `transient_credentials` and unknown fields. No live transient proof carrier is reintroduced. | This is source-envelope evidence, not proof of every installed client or service composition. |
| Service-owned twin/re-pin/discovery | Current Vuoro source composition locks Sprintctl **0.20.0**, source `bef3902c7ce76378ceb6155fe4f73ba957fc00c6`, wheel SHA-256 `3c04f265ffc8cbc4c1720d753186000dced40c584d775a59afb176c2502b9025`. Historical native-reserve composition evidence is retained in Vuoro. | The Oct-9 composition result explicitly leaves OCI/deployed-runtime/enrollment evidence unknown. Current source pins are not deployment receipts. Fresh `vuoro#1243` could not be found; counterpart completion remains unverified. |

The correctly scoped twin query ran from the real `/projects/dev/vuoro`
checkout and returned `item-not-found` for **`vuoro#1243`**. A current Vuoro
item-list call returned 102 rows, with no matching reservation/catalog-v2/
transient-credential/1243 title in that returned set. This does not establish
that no historical or migrated counterpart exists. No other repository's
numeric #1243 was substituted.

The reference guide's legacy paragraph naming `work.claim.start`,
`work.claim.arbitrate` and `claim.acquire` as the retry route is stale relative
to this catalog and the current native reserve owner. It is identified here
for the guidance owner; it is not authority to recreate those operations.

## Executed evidence and limits

- **83 local tests passed in 2.05 seconds**: authority contracts, native reserve
  metadata, advisory reservations, and the SQLite archive/drop migration
  example in `tests/test_core.py::TestEdgeCases`.
- **50 disposable PostgreSQL 16.15 cases passed in 5.50 seconds**: 28 native
  owner cases, 21 native intake cases, and one one-off schema observation
  belonging to #1238. All three fixture cleanups left zero rows. The replay
  and concurrency claims apply to those owner/intake examples, not to every
  operation or an external exactly-once effect.
- **6 Vuoro static envelope tests passed in 0.13 seconds** using the source
  contract at the pinned Vuoro revision in the isolated Sprintctl verification
  environment. No HTTP server, released-wheel composition gate or deployed
  authentication was exercised by that source-only run.

Relevant anchors include `tests/test_native_reserve_owner.py`,
`tests/pg/test_native_reserve_owner.py`,
`tests/pg/test_native_reserve_intake.py`, `served.reservation_operation`,
`WorkApplication._reservation_reserve_v1`, and Vuoro's
`packages/vuoro-service/tests/test_invocation_fields.py`.

Owner review cleared publication of these observations only. Resolve the
historical criterion spelling/current owner mapping and counterpart
qualification before proposing any terminal disposition. No API, schema,
catalog, actor, grant, dependency, archive policy or native status was changed.
