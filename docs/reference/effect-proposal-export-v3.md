# Proposal-associated verification export v3

`work.effect.export-proposal-v3` is a readonly source-content projection for the
protected Q4 acceptance boundary. It adds existing native verification receipts
to v2's proposal/run/work/Release subset. It does not accept, apply, authenticate
a public person or attest check execution. Source publication does not prove
production migration, credentials, deployment or protected activation.

The closed request remains `{intent_id, intent_revision}`. The owner requires
`work.effect.get`, `work:read`, authenticated repository selection and a workspace
binding. Native proposal revision is immutable (currently 1), including across
state transitions; changing content means a new native proposal. No selection
ceremony or new public write operation is introduced. The existing receipt's
single observation claim and detail already name its proposal. Existing evidence
append ownership and generic owned-run read APIs remain unchanged.

The closed response schema is `sprintctl-effect-proposal-export/v3`. Root fields
are v2's `schema`, `repo_id`, `workspace_id`, `intent`, `run`, `work`, `release`,
plus `verification_evidence`. All v2 base objects retain their exact fields. V2
itself remains byte-compatible and still refuses verification-required/map
contracts; selecting v3 is explicit in the protected adapter's successor profile.

Every exported evidence entry has exactly:

| Object | Fields |
| --- | --- |
| entry | `run`, `item`, `native_claim`, `receipt`, `entry_digest` |
| run | `run_id`, `principal_id`, `workspace_id`, `client_id`, `grant_id` |
| item | `item_id`, `kind`, `digest`, `chain_seq`, `chain_prev_digest` |
| native_claim | `claim_type`, `subject`, `grant_id`, `confirms` |
| receipt | `schema`, `intent_id`, `intent_revision`, `canonical_intent_digest`, `release_digest`, `artifact`, `checks` |
| artifact | `domain`, `digest` |
| check | `name`, `revision`, `status` |

The run is the actual receipt writer's stored binding, not the current reader's
run or the proposer/operator's claimed identity. The only cross-run disclosure is
this bounded proposal-associated verification subset within the selected
repository/workspace. Missing nullable claim grant/confirms fields normalize to
null under existing native `.get(None)` semantics; no identity or detail is
fabricated. Collector, general provenance, raw logs, ref, freshness, private notes
and unrelated claims are excluded.

One PostgreSQL `REPEATABLE READ READ ONLY` sibling snapshot covers the proposal,
work, full current Release and every candidate. Candidates are every
`protected-artifact-verification` row in the selected repository whose claims
array contains an exact matching proposal subject, joined to a run in the
selected workspace. The containment lookup considers **any claim position**.
A candidate with multiple claims, wrong detail ID/revision/digest/Release/artifact,
failed/stale checks or malformed data refuses the entire bundle; a good row
cannot hide it. A missing joined run is included and refuses, even if a source FK
was bypassed. Valid foreign-workspace runs and other proposal subjects are outside
the permitted projection. No foreign-run resolver, arbitrary evidence selector,
latest/best receipt choice or person-equivalence test is used.

Order is native run ID using PostgreSQL C collation, chain sequence and evidence
item ID using C collation. The query fetches at most 30; 30 candidates refuse
rather than truncate. At most 29 receipts leave room for the three mandatory V5
run/work/Release refs within the 32-ref ceiling. New receipt append changes only
future exports and canonical hashes; already prepared or protected accepted
copies retain their frozen bytes and refs.

Native verification is required when `effect_verification_required` is true
**or** `effect_check_revisions` is present. Zero receipts refuse in either case.
An explicit false boolean with a present map stays false in the exported contract;
it is never rewritten. Every candidate must satisfy native
`sprintctl-protected-artifact-verification/v1`: exact integer revision, legacy
content digest, current Release, UTF-8 diff artifact, one observation claim,
null grant/confirms, one to 64 unique passed revision-bound checks and any frozen
check map. Additional passed checks are permitted. Optional verification does
not permit silently dropping bad associated candidates.

Both native hashes and V5 hashes are recomputed without changing either domain.
Native receipt digest uses UTF-8 JSON with `ensure_ascii=False`, matching V5;
Unicode check names are supported when the exact digests agree. Native chain
entry digest uses `ensure_ascii=True`, while V5 uses canonical UTF-8. An evidence
item ID containing Unicode may therefore be valid native evidence but is
explicitly refused as `proposal-verification-hash-domain-unsupported` when the
hashes differ. The exporter neither normalizes IDs nor substitutes a new hash.
This is a supported native subset, not full Unicode/native evidence coverage.
The chain entry digest binds the selected item's identity/digest/position and
predecessor; it is not a fresh full-chain execution attestation.

Bounds remain canonical native output 2 MiB, raw transport 4 MiB, depth 32 and
safe integers ±(2^53−1). Namespace/run/principal/client/grant/evidence IDs and
check names are capped at 200 characters; contracts allow 64 obligations of
200 characters, 64 check revisions and 128 context refs. V2 parameter bounds and
exact full native digest/revision validation remain unchanged. No truncation.

## Explicit schema24 migration

PostgreSQL schema24 adds only
`idx_evidence_item_effect_verification_subject`, a partial GIN index over
`claims jsonb_path_ops` where kind is `protected-artifact-verification`.
Its existing name is admitted only with the exact table, method, key, opclass,
predicate and ready/valid/nonunique shape. A collision refuses. Migration is
explicit and transactional; normal startup and v3 reads never install or repair
it. The v3 exporter checks for exactly one schema-version row equal to 24, 25, 26, 27, or 28
and a healthy index. This operation-level check does not widen native runtime
admission: the running owner must also satisfy the exact compatibility policy in
[pg_migrations.py](../../sprintctl/pg_migrations.py). The exporter guard is in
[effect_intent_export.py](../../sprintctl/effect_intent_export.py). SQLite's
separate schema lineage is unchanged and both exports refuse SQLite as unsupported.

The original rollout from schema 23 to schema 24 required a schema 24 runtime; its older
schema 23 runtime refused the migrated database. The rollback procedure for that
original transition removes only this index and restores schema 23 before starting
the old runtime, after verifying other schema/data are unchanged. It applies only
when no later migrations are present; it is not a rollback procedure for schemas 25
through 28. No automatic schema downgrade is provided. Source compatibility does
not establish a deployed runtime's version or qualification.

Qualification lives in `tests/pg/test_effect_verification_export.py` and
`test_effect_verification_export_migration.py`, using actual disposable
PostgreSQL16. It covers required/map-only/optional cases, native-owned append
without selection, candidate poisoning, namespace visibility, orphan runs,
Unicode hash-domain cases, deterministic overflow, concurrent append snapshots,
explicit upgrade/index collisions and absence of runtime DDL. Existing v2 and
catalog descriptor regressions remain separate gates. Broker v3 profile and
protected consumer activation are separate successors.
