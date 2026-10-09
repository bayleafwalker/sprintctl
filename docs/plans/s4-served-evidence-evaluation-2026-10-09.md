# S4 served evaluation: chain and Release read prerequisite

This implements the read-time validity and coherent-snapshot prerequisite of
[agentops S4](https://github.com/bayleafwalker/agentops/blob/46024572e7f01cefb05443affa0da9e428a37316/docs/plans/2026-10-03-s4-evidence-home-preparation.md).
The complete goal still requires authenticated execution facts, real inventory
import, production writer qualification, cutover and soak. This increment cannot
establish accepted execution, grant use, non-invocation or independent completion.

`work.evidence.evaluate-v1` is read-only and accepts no idempotency key. Its primary
catalog capability is `work:read`; the owner additionally requires `work:evidence`.
Both are existing capabilities. The run must match the authenticated principal,
workspace, client and grant in the requested repository. Caller-supplied identity,
trust or execution-fact fields are refused.

The six closed arguments are `run_id`, `subject`, `basis`, `as_of`,
`current_input_digests` and `expected_tail`. Basis contains item ID, full Release
revision (including revise count), and recorded Release digest. Subject is an
opaque effect correlation ID, not automatically an item, proposal or OAuth grant.
The input map is caller-supplied observation, never attested current-world truth.
An expected tail is exactly item ID/sequence/entry digest; null requires an empty
chain. A different tail, broken chain, oversized or incomplete source refuses.

A sibling connection reads the exact run binding, complete chain and current
item/recorded Release basis in one read-only repeatable-read transaction. It
neither commits nor rolls back a caller-owned transaction. An SQL size/count
preflight occurs before fetching full rows. Bounds are 100,000 chain items and
32 MiB of complete source/snapshot; subject is at most 512 characters; input maps
contain at most 256 nonempty keys/values of at most 256 characters. SQL statements
have a five-second local timeout. Sources beyond the bounds are not truncated.
The sibling connection closes on success, conflict and refusal.

The response names the evaluator revision, normalized zoned `as_of`, observation
time, full run binding, requested/current basis, evaluated inputs and their
caller-supplied assurance, source watermarks, complete content snapshot SHA256,
per-item validity, and separate authored assertions. Snapshot hashing includes
claims, provenance and validity as well as chain positions/content digests; the
four-field chain entry hash alone cannot bind those fields. Receipt observation
time and evaluation inputs/clock are reported separately from captured source
content. `as_of` evaluates the captured source; it does not reconstruct a database
as it existed in the past. A changed full item revision or mismatched recorded
Release revision remains stale even if a caller supplies the new item revision
alongside an old frozen Release digest.

## Versioned validity policy

Both `valid_from` and a bounded `valid_until` are inclusive. Before `valid_from`
an assertion is not yet valid; after `valid_until` it is expired. Every timestamp
must include a timezone and is normalized to UTC. Invalid intervals, unsupported
bases or malformed windows are explicitly invalid. Indefinite validity states
that the content-addressed assertion remains available; it does not establish
current-world truth. `until_inputs_change` requires at least one component.
Missing evaluated components are unknown, differing supplied components are
changed, and exact supplied matches are valid. Missing is never treated as a
match. Original payloads/digests and historical Decisions remain untouched.

## Execution facts remain an explicit prerequisite

This version returns empty `authenticated_execution_facts`, unsupported authority
coverage, unknown effect state and `reconcile`. It always returns
`authorizes_execution:false`. Authored success, grant-use, observation or
non-invocation claims, collector names and provenance trust labels cannot fill
the missing source or authorize reacquisition/execution. The response schema
pins those limits rather than permitting invented fact records.

A subsequent independently reviewed owner source must correlate immutable
accepted-attempt, invocation/redemption, pre-invocation refusal, complete-scope
non-invocation proof and independent terminal observations with actual authenticated
issuers and source watermarks. OAuth `run.grant_id`, work leases, proposal
acceptance and reconciler exceptions do not establish those facts. The required
accepted+used+dead and accepted+missing-use histories must reconcile unless
independent terminal/non-invocation authority resolves them; expiry or lease loss
cannot erase occurrence or uncertainty. A caller cannot nominate a trusted source.
Adding such authority requires its own concrete source contract and qualification;
this read must not silently grow a second Decision writer or execution driver.
