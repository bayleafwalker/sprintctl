---
doc_id: result-read-packet
status: draft
supersedes: null
---

# Independently inspectable owner-read packets

Native parent: `sprintctl#2662`. The owner approved continuing implementation on
2026-10-18. This is a bounded offline read artifact, not fulfillment of the broader
SP-K02 complete request/attempt/target/terminal-Decision transfer requirement.

## Frozen scope

`result packet create --acceptance FILE [--continuation FILE] --output FILE`
packages complete supplied existing acceptance-snapshot/v1 and optional
continuation-delta/v1 responses. `result packet inspect FILE
[--expected-sha256 sha256:HEX] --format text|json` opens that same artifact.
Both commands are local client utilities and perform zero owner, artifact or
network calls. They never open a database or import rows. A caller may take a
packet to another reader/host; no source identity or authority travels with it.

The packet is `sprintctl-result-read-packet/v1`, serializer version1. It contains
an explicit caller-clock UTC packaging timestamp, supported operation/schema
names, complete responses and a canonical SHA256. Canonical bytes are UTF-8 JSON
with sorted keys, separators comma/colon, no nonfinite numbers or invalid Unicode;
only `packet_sha256` is excluded from its own digest domain. The optional
caller-declared source reference is opaque metadata and is never resolved or
validated as authentic. Owner digests retain their original domains.

Input parsing rejects recursive duplicate keys, unsupported versions, unknown
packet fields, invalid/nonfinite JSON and excessive nesting. Raw input and final
packet size are each bounded to2MiB; nesting is bounded to64 container levels.
Responses validate against the exact existing published owner result schemas;
extra or missing fields cannot silently masquerade as a supported response.
Schema validation uses an optional `inspection` extra backed by the existing
locked jsonschema dependency. The core CLI does not add a second schema engine.

The two supplied responses must identify the same repository/item. Different
Release records are inspectable discrepancies, not silent selection or malformed
input. Cross-response atomicity/currentness is never established by packaging.

## Integrity and interpretation

Inspection separates internally consistent digest, independent expected digest
match/mismatch/unavailable, owner snapshot identities and supplied-source
**unverified** status. Tampering plus recomputing the packet's self digest cannot
match the old independent expectation. A self-consistent packet without such an
expectation has no independent integrity qualification. A match authenticates
neither the owner nor a caller-declared source reference.

The report retains the existing effect explanation and optional continuation
brief, exact requested-basis/artifact/receipt comparisons, completeness and
unknowns. Receipt matching cannot hide requested artifact mismatch. Omitted and
caller-invisible receipts stay distinct. An accepted/applied intent and a done
item do not fill terminal Decision binding, actor/time, attempt history, target
acknowledgment/observation or lease-settlement gaps. No authority is conferred.
Valid conflicting content inspects successfully with explicit mismatches; malformed
format/schema fails. Expected-digest mismatch is reported and exits nonzero.

## Transfer boundary and acceptance

No archive guard, existing export/import path, database schema, catalog operation,
credential, grant or release pin changes. The separately guarded
[archival fixture](2630-archival-result-transfer.md) retains its private database,
closure, capability-refusal and empty-target guarantees. This packet has no owner
import/ingress or general historical export endpoint, external-byte custody,
current-access guarantee or complete accepted-result reconstruction claim.

Acceptance covers canonical round trips; changed bytes and tamper+rehash against
independent expectation; malformed/unsupported/duplicate/nonfinite/deep/oversized
input; same-item correspondence and different Releases; receipt missingness,
truncated pages and unchanged raw response retention; no network/artifact/owner
access; escaped untrusted text; and no acceptance/target/authority promotion.
Existing archival guards remain unchanged. Main CI and independent review gate
landing. The broader parent remains open where joins/transfer are unqualified.
