# Readonly Q4 proposal export, initial subset

`work.effect.export-proposal-v2` returns source assertions for a protected-side
Q4 adapter. It grants no acceptance, execution or identity assurance. Publication
of this source operation does not establish deployment or protected Q4 activation.

The closed request is `{intent_id, intent_revision}`. The positive integer
revision must name the exact proposal still in `proposed` state. The catalog
requires `work.effect.get`; the owner additionally requires `work:read` and an
authenticated principal/workspace binding. Repository selection uses the existing
served context and repository authorization, never an operation argument.

All queries use one PostgreSQL sibling connection with `REPEATABLE READ READ
ONLY`. SQLite refuses `unsupported-backend`; there is no double-read fallback.
The selected proposal supplies the only run ID and work item ID queried. The run
must exist in the selected repository and authenticated workspace. Its principal,
client and grant need not equal the reader's: this is a proposal-bound minimal
projection, not a change to the caller-owned `work.run.resolve-v1` API. No other
run, evidence, session note or private metadata is exposed.

The response has exactly these fields:

| Object | Exact fields |
| --- | --- |
| root | `schema`, `repo_id`, `workspace_id`, `intent`, `run`, `work`, `release` |
| intent | `intent_id`, `revision`, `canonical_intent_digest`, `release_digest`, `item_id`, `repository`, `base_commit`, `title`, `rationale`, `unified_diff`, `run_id` |
| run | `run_id`, `principal_id`, `workspace_id`, `client_id`, `grant_id` |
| work | `work_item_id`, `aggregate_uuid`, `edit_revision`, `status_revision` |
| release | `work_item_id`, `item_revision`, `release_digest`, `acceptance_contract`, `context_refs` |

Root schema is `sprintctl-effect-proposal-export/v2`. The owner copies actual
nullable client/grant values. Work revision/status/UUID come from native work
storage; Release fields come from the current frozen Release. A proposal without
an explicit current Release refuses. The full current release revision, including
the revise-decision count, must equal the frozen revision. Native six-field
`sprintctl-effect-intent/v1` content digest and the full native Release digest are
recomputed without changing their domains. Context refs retain their canonical
native ordering and all `ref_type`, `url`, `label` fields.

Supported contract keys are `review_required`, `verification_profile`,
`evidence_obligations`, `effect_verification_required`, `effect_check_revisions`.
Unknown keys refuse rather than being dropped. Native contract normalization
applies; supported profiles are `checked` and `self-reported`. Review required is
valid and defaults to true in native releases; it does not trigger an export
refusal.

This initial export supplies inputs for exactly three V5 envelopes: run, work,
Release. If `effect_verification_required` is true **or** a check revision map is
present, it refuses `proposal-verification-selection-unavailable`. No selected
verification ref is stored on a proposed native intent today; public accepted
`verification_binding` is not a replacement. A later explicit proposal-bound
immutable verification selection extension needs its own version, schema,
visibility contract and tests. It must not scan arbitrary foreign runs or treat
public accepted metadata as protected authority.

Native canonical output is at most 2 MiB, depth 32, integer range ±(2^53−1), with
no floats, lone surrogates or unsupported object-key ordering. Native parameter
limits remain title/repository 200 characters, rationale 8000, diff 1,000,000;
UTF-8 output bounds also apply. Namespace/run/principal/client/grant IDs are at
most 200 characters. Context refs are capped at 128; obligations at 64 labels of
200 characters. Bounds refuse; content is never truncated. The protected adapter
separately limits raw HTTP to 4 MiB and canonical/envelope bytes to 2 MiB, rejects
malformed or duplicate JSON members, and validates the exact V5 joins.

The protected registry supplies authority mapping, exact repository locator,
HTTPS origin/CA, readonly credential reference and retrieval profile ID/revision.
These are observations of configuration and bytes, not public identity
attestation. The adapter creates immutable digest-pinned run/work/Release refs,
computes the separate Q4 canonical hash and displays protected-rendered content
before separately authenticated operator approval. No native public acceptance
or application metadata enters this export.

Fixed refusal codes include `proposal-export-unavailable`,
`proposal-export-stale`, `proposal-export-release-stale`,
`proposal-export-unsupported-contract`, `proposal-export-invalid`,
`proposal-export-size-bound`, `proposal-verification-selection-unavailable`.
Refusals disclose no proposal text. Authentication/authority and backend refusal
use the existing owner codes.

Qualification is in `tests/pg/test_effect_intent_export.py` with the owner’s
actual disposable PostgreSQL 16 harness. It proves readonly write rejection and
coherent output across a concurrent native edit, plus cross-principal authorized
projection, namespace/repository refusal, stale revision/Release, changed revise
count, unsupported contract, and required-verification refusal. SQLite routing
and closed catalog request checks are in `tests/test_work_application.py`.
