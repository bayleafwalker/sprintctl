# Hosted run invocation receipt contract

Status: proposed v1 contract, contract-only. This document defines an
additive owner-side correlation capability. It does not enable Cloud
population accounting or make invocation correlation an acceptance condition.

## Operations

`work.run.register-v1` remains unchanged. Its request schema, `work:evidence`
authority, idempotency key, canonical ledger tool name (`register_run`),
request-digest construction, domain result, and run authority remain exactly
as currently published. A caller without verified invocation metadata uses
that operation. Historical and uncorrelated idempotent replays stay
uncorrelated; they are never retroactively adopted into a hosted invocation.

`work.run.register-correlated-v1` is additive and takes the exact same
arguments as `work.run.register-v1`, including the optional
`predecessor_run_id`. It does not accept invocation IDs, attempt IDs, an epoch,
or other correlation fields in MCP arguments. Those values are taken only
from the verified gateway identity metadata. The correlated operation
requires both valid IDs and the verified `principal_epoch`; an absent or
partial pair is a typed refusal before any owner write. The gateway must use
the legacy operation when no admitted signed pair exists. The correlated
operation preserves the existing `work:evidence` authorization and all
existing run checks. Its result retains the exact legacy `{repo_id, run}`
fields and adds `correlation_status` and `receipt`. A newly bound or exact
correlated replay returns `bound` and the exact receipt. If an idempotent
replay resolves to a legacy run that predates correlation, the result is
`legacy-unbound` with a null receipt; it never attaches that run to the
current invocation. Missing or partial metadata on the correlated operation
is a typed refusal, not a silent fallback.

The owner calls the existing idempotent write path with the canonical tool
name `register_run`, the original arguments, and the original identity-derived
client/grant digest binding. Correlation must not enter the request digest,
ledger key, or stored v1 result. An optional in-transaction callback receives
the cursor, finalized legacy v1 result, and replay flag after the ledger
digest has been checked, for both a new write and an exact replay, but before
commit. It may append correlation rows and produce a separate receipt without
changing the stored v1 result. A
digest conflict never invokes it. With the callback omitted, idempotent-write
behavior and results are unchanged. If correlation persistence fails, the
transaction rolls back the owner write and ledger claim as a technical
failure; the caller receives a retryable/unknown transport result, never a
semantic run rejection.

`work.run.invocation-receipt-get-v1` is an exact lookup, not a list or a
search. It requires `work:read`, the currently authenticated verified
identity, and exact `run_id`, `invocation_id`, and `attempt_id`. The owner
first resolves the run through its existing current owned-run check, then
looks up the exact triple. The repository is the
explicit selected repository in the owner invocation context; there is no
default in a multi-repository context. The operation joins only that exact
triple to the run linked by successful registration. It does not
enumerate runs, infer a run from an item, timestamp, idempotency key, or
caller-supplied owner ID, or return a “current” receipt.

For both reads and writes, the correlation namespace is the exact tuple
`(repo_id, workspace_id, principal_id, principal_epoch, client_id, grant_id)`.
`principal_id` is the existing owner-composed value from verified issuer,
subject, and epoch; use the existing identity composition as the oracle,
never hand-compose it or infer an epoch from a run row. The epoch is stored
from verified identity metadata and compared with the current verified
identity on receipt reads. Nullable client/grant values compare exactly,
including null. Correlation adds no permission: registration still passes
the existing owner authorization and domain checks, and reads additionally
pass `work:read` and the existing owned-run check. Unknown run IDs and every
foreign repo/workspace/principal/epoch/client/grant binding return the same
`run-not-found`/404 response. For a currently owned run with no matching
invocation/attempt receipt, return the distinct `correlation-absent` result;
do not search or retroactively adopt an uncorrelated run.

## Schema 23 storage invariants

Schema 23 adds append-only correlation storage. A logical invocation binds to
one successful run owner record, one canonical owner request digest, and one
complete namespace tuple. Each admitted delivery attempt is a separate row
with a globally unique attempt ID and an exact reference to that logical
invocation. These rows are provenance, not execution, downstream-effect, or
acceptance evidence.

An invocation ID cannot be rebound to another request digest, run, repo,
workspace, principal, epoch, client, or grant. A retry may append a new
attempt only when the owner idempotency replay resolves to the exact same
logical invocation and stored request digest and the full verified namespace
matches. A global attempt-ID reuse is idempotent only for the identical
invocation and complete binding; every different binding is a typed
correlation conflict. Partial, unsigned, malformed, unverified, or
cross-bound identity metadata writes no correlation row. No request digest,
idempotency key, raw arguments, token, patch, or response body is added to
the public receipt or logs.

`owner_record_digest` is the lowercase 64-hex SHA-256 required by the Cloud
journal schema. It is
`SHA256(UTF8("sprintctl-run-owner-record/v1\0") || JCS(projection))`, where
JCS is RFC 8785 under the Cloud contract's I-JSON safe-integer profile. The
projection contains `repo_id`, `owner_record_kind: "run"`, `run_id`,
`principal_id`, verified `principal_epoch`, `workspace_id`, `client_id`,
`grant_id`, `harness_id`, `harness_build`, `model_id`, `recipe_id`,
`observed_profile`, `predecessor_run_id` (normalized to null when absent),
`created_at`, and the private `owner_request_digest`. Normalize the timestamp
to UTC and serialize exactly as `YYYY-MM-DDTHH:mm:ss.ffffffZ`. JCS sorts
property names by UTF-16 code units, applies RFC 8785 string escaping, accepts
only integers in `[-9007199254740991, 9007199254740991]`, and rejects floats,
non-finite numbers, invalid Unicode, and out-of-range integers. See the
machine-readable vectors in `owner-record-digest-vectors.json`. Exclude
`grant_ids` and `claim_ids` because they are mutable. Never emit the raw
request digest, idempotency key, or run arguments in a receipt, audit, or log;
the private request digest is an input only to this integrity commitment and
internal retry binding.

The storage relations are globally addressable by the opaque invocation and
attempt IDs, while all reads and uniqueness checks remain bound to `repo_id`
and the complete owner namespace. One invocation row references exactly one
run row and stores the internal original owner request digest plus immutable
owner-record digest and binding tuple. Each attempt row references that
invocation and repeats the exact binding tuple used at admission. Attempt IDs
are globally unique. Foreign keys and uniqueness constraints prevent a run
from joining to multiple invocation rows or an attempt from attaching to a
different repository/run namespace. Database permissions/triggers reject
update and delete of committed correlation rows; migration-owner maintenance
is separate and reviewed. No raw idempotency key is added to these relations.

The receipt result has the exact shape in
`../../verification/fixtures/run-invocation-receipt-v1/receipt-result.schema.json`.
It contains schema version, invocation and attempt IDs, the run owner record
kind and ID, immutable owner-record digest, and the exact current owner
binding. It contains no run arguments, idempotency key, request digest,
mutable claim/grant lists, or broad owner response.

## Authority and migration boundary

No existing effect-verification or work-acceptance predicate may read these
correlations. Missing correlation means hosted reconstruction is incomplete;
it does not reject an otherwise authorized run registration. Correlation
cannot satisfy permissions or prove semantic execution. The edge must send
the signed IDs only after Cloud admission; Sprintctl verifies them through
the configured identity trust path. Cloud population counting, sealed
journal projection, and any effect-proposal correlation require separate
contracts and qualification. This contract enables only run registration.

Schema 23 is additive and PostgreSQL-only. The local SQLite backend has no
run/idempotency storage; these operations remain explicitly unsupported there
unless a future separately reviewed implementation adds the actual data
model. No migration is applied by this contract packet.
