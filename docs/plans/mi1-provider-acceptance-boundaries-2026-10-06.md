# MI-1 provider acceptance boundaries

Served agentops P2 item 2613. Additive verification over the existing native
owner; no product semantics, public capabilities or runtime pins change.
Governing contract: Vuoro market-integration plan at
`f9cadd85b35cd28717c5ab3fc24409bcb0f7b98f`, P2/P3; existing owner contract
`sprintctl/effect_intent.py::canonical_intent_digest`.

## Digest domains and measured owner horizon

A provider-reported artifact hash, a measured patch-byte hash and the owner
canonical intent digest are separate domains. The current `EffectIntent`
acceptance binds the canonical digest of item/repository/base/title/rationale
and unified diff, not an unqualified hash of a provider observation. The
existing public-role denial alone was too weak to prove this trust-side
binding, so the new history uses the trusted acceptor with genuine acceptance
capability. It records a synthetic success observation whose reported artifact
hash differs from the proposed patch bytes, then tries both its artifact hash
and its observation hash as the acceptance binding. Both are refused
`effect-digest-mismatch`/409 without changing work, intent or evidence tail.

A positive control accepts the actual canonical frozen intent. This shows the
negative checks exercised digest binding rather than denying all actors. It
also states the limit: an independently authorized trusted actor can accept
the intent while the incompatible provider observation remains recorded.
These tests do not pretend that the native owner already checks a protected
verifier's raw-artifact receipt or a release-to-artifact link. Those missing
P1/P3/Track B links remain visible work, not a stronger acceptance claim.

## Additive removal boundary

A second history makes provider decoder imports fail, with a direct failure
control, and creates a fresh owner facade. Native run binding, stored claims,
effect identity, work state and idempotency ledger remain unchanged and
readable. Original-byte recapture is the same request; confirmed producer sync
performs no append. Public acceptance remains refused. This proves the stated
boundary over admitted data; it does not test uninstalling a deployed service
package or losing the original referenced artifact.

Both new synthetic deliveries are frozen outputs of the actual Vuoro decoder
at `8c5b64f3a9b295a68273cab32fdcd9c9527e36b4`, separately fingerprinted in
the fixture. Original four fixture cases and provenance stay intact. Reusing
the original case's delivery key initially collided with the owner ledger,
which intentionally outlives test work cleanup; distinct deliveries fix the
fixture without weakening replay semantics or changing owner code.

Focused verification: all six provider histories passed on disposable
PostgreSQL16.15, including the existing reply-loss, content-conflict,
public-boundary and independent-connection contention histories. Fixture
cleanup reports zero remaining work rows. Broader suite and exact-head CI
outcomes are recorded in the result packet when measured.
