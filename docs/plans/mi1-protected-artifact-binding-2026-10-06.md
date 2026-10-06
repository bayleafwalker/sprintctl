# MI-1 protected artifact binding in the native work owner

Implementation increment for served agentops#2613 (P2), with P1 reconstruction
and P3/Track B consumers. Governing direction is the Vuoro MI-1 plan at
`f9cadd85b35cd28717c5ab3fc24409bcb0f7b98f`, and agentops target TS-1/2/16.
This is a source contract and measured owner proof, not deployment acceptance
or a hosted commercial harness demonstration.

## Stored bindings and authority

A new proposal snapshots the current work `release_digest`, when one exists.
The existing v1 canonical intent digest remains unchanged: its domain includes
the unified diff and intent content, while the Release freezes work requirements.
A protected receipt explicitly relates these distinct domains to one intent ID
and revision. A change is a new proposal; neither history nor old digests are
rewritten.

An execution reservation may freeze `effect_verification_required: true` in its
acceptance contract. Only a boolean is accepted. The existing private
`work.effect.accept-v1` operation accepts an optional
`verification_ref: {run_id, item_id}`. The authenticated acceptor must own that
native run with the exact principal, workspace, OAuth client and grant binding.
Ordinary evidence-writing authority cannot qualify somebody else's evidence
as the protected acceptor's receipt. No capability, operation or public apply
path is added.

The receipt kind is `protected-artifact-verification`, with exactly one
observation claim, subject equal to the intent ID, and null grant/confirms. Its
exact detail contains schema `sprintctl-protected-artifact-verification/v1`,
intent ID/revision, canonical intent digest, Release digest, an artifact
`{domain: "utf8-unified-diff/v1", digest: "sha256:..."}`, and one to 64 unique
named checks with SHA-256 check revisions and `status: "passed"`. The evidence
digest must equal SHA-256 of sorted compact UTF-8 JSON of this detail. The owner
independently computes SHA-256 of the actual stored UTF-8 unified diff. Provider
artifact hashes, canonical intent hashes and receipt-body hashes are not
interchangeable.

Checks are assertions by the protected actor, not cryptographic execution
attestations or an automatically selected verifier policy. The future protected
consumer must actually run the declared checks. This increment does not make
provider success a work Decision or close the work item.

## Commit and migration boundaries

Acceptance takes the existing intent lock, then the work item lock, and checks
that its proposal Release is still current and describes the actual current work
revision. The requirement is checked from both the frozen proposal Release and
the current Release. A changed Release or description refuses acceptance even
if the newer requirement is downgraded. Receipt validation and immutable proof
storage happen in the same owner transaction. Application also rechecks this
Release/revision and requires the stored proof when the contract requires it.

Acceptance freezes the evidence reference, evidence/entry digests, authenticated
verifier binding and exact checked detail. A separate database trigger makes
proposal Release and accepted verification metadata immutable, including during
an otherwise legal accepted-to-applied transition. The schema-19 content/state
guard remains intact. Legacy records retain absent Release/proof fields;
migration does not manufacture links or change their canonical digests.

Remote schema 21 and source version 0.13.0 are a coordinated cutover. Runtime
admission is exactly schema 21, and startup remains read-only. The deployment
migrator adds nullable columns and refuses pre-existing binding columns,
including matching-type foreign columns. Failed migration preserves the old
ledger; an already completed migration is a no-op. Historical migration fixtures
check older shapes at their actual boundary rather than pretending schema 21
is schema 19. A schema-20 runtime refuses schema 21; rolling back requires a
coordinated migration/runtime plan, not starting an old writer.

## Verification horizon and remaining integration

The new required-receipt oracle fails on unchanged baseline
`1a664ea0fff3bbc15142554855c8a80fcc255ea2`: it accepts without any receipt.
Positive and adversarial histories run through the actual native owner on
isolated PostgreSQL 16. Two acceptors contend inside the transaction before its
state write; a competing work edit waits for acceptance commit and then makes
application stale. The initial race hook paused after the acceptance transaction
had already committed; moving the pause inside the verification guard corrects
the experiment, not product semantics. Malformed references/checks, mismatched
raw bytes/intent/Release/digest, provider kind and foreign native identities
refuse without changing the proposal or deleting recorded evidence.

The result packet records measured suites and their exact source fingerprints.
Before MI-1 end-to-end acceptance, integrate the protected verifier and existing
reconciler preflight, publish/pin the owner wheel, coordinate schema migration
and service rollout, and reconstruct these links in P1. Owner application
recording is not atomic with external Git effects; retain the existing
reconciler's uncertainty/recovery handling. P2 stays active and Track B remains
unqualified until the real runtime and commercial harness evidence is present.

## Release gate correction

The immutable 0.13.0 tag's release run passed Python 3.11 but failed Python
3.12: two native served-refusal tests assumed the optional client was installed,
while the release gate synchronized only development extras. No wheel was
published. Patch 0.13.1 preserves schema 21 and all owner semantics, explicitly
installs the served extra in release gates, and makes those transport tests skip
when that optional client is absent. A development-only control passed 31 tests
with the two expected skips; the served installation passed the full 1,729-test
unit suite. The failed tag remains unchanged; the patch uses a new immutable tag.
