# Private request-resolution lookup and receipt readback

Status: source-only, default-off design. No route is mounted by default, no
public WorkOperation or catalog entry exists, and no production audience,
transport identity, network policy, or key is registered by this source.

## Trust boundary

The adapter verifies the existing gateway-signed Vuoro identity with the
dedicated scalar audience `sprintctl-operator-request-delivery-read/v1`, then
derives requester principal, workspace, authorities, and repository scope from
the resolved runtime `Identity`. The body supplies only owner selectors
(request ID, opaque binding ID, or prior invocation ID), never recipient
provenance or authority fields. Sprintctl rechecks exact request ownership using that
verified principal and workspace in the owner database. Foreign, absent, stale,
and withdrawn owner data is not distinguished in responses.

The gateway assertion authenticates identity and invocation ID; it does not
cryptographically bind the body request ID. A caller can substitute an ID in
its own authenticated scope, so owner authorization is mandatory on every
read. Reusing a fresh assertion at another receiver process may pass the
gateway resolver's process-local replay cache. These routes are read-only and
repeat the owner authorization on every request; they do not claim durable
one-use invocation semantics. They must not be enabled until the private
composition independently authenticates the trusted Cloud admission service
caller through `trusted_cloud_adapter_guard`, in addition to the process-owned
private ingress guard. Neither guard is supplied by this module.

The trusted adapter guard must verify a distinct Cloud service assertion, not
a caller-controlled header or the end-user gateway assertion. The source
composition contract uses a dedicated Ed25519 key and scalar audience
`sprintctl-trusted-cloud-adapter/v1`, pins issuer and key ID, binds the exact
workspace, `POST` path, and SHA-256 of the bounded request bytes, and limits
assertions to 30 seconds. The end-user gateway assertion remains a separate
header and continues to authorize the owner read. The service assertion JTI
cache is per process; it is not cluster-wide one-use. The durable owner does
not rely on it for read authorization or delivery dedupe.

The lookup returns current owner coordinates and persisted recipient consent
provenance only to that separately authenticated Cloud admission component.
It includes the original verified OAuth scopes and verified internal
authorities needed for Cloud's live-grant check. `effective_authority` remains
the separate owner-enforced delivery ceiling and is exactly `work:read`; the
lookup never derives or adds authority. The component must not proxy those
provenance fields to the human requester. Token and refresh secrets are never
stored or returned.

The exact lookup accepts an owner-known opaque binding ID, not a recipient
principal or work/revision coordinates. Cloud derives the admission assertion's
`caller` from its independently verified gateway identity. It must not accept
a caller principal from this request body or synthesize the recipient
authority ceiling from other fields.

## Resolution lookup

`POST /internal/operator-request-delivery/v1/resolve` accepts the private
gateway envelope plus `request_id` and one owner-known `binding_id`. Sprintctl
locks the owned request and exact active binding, derives the work ID, binding
revision and current work status revision from owner rows, then rechecks the
coordinates through the existing owner admission lookup. This preserves
request/binding/instance/work lock order and current status, consent, and
provenance checks. A stale, withdrawn, foreign, or absent binding has the same
generic refusal. Owner integrity or storage errors fail closed. Resolution
performs no inserts or updates and never enumerates a recipient catalog.

Freshness is checked before and after the owner lookup. The exact selector
keeps each read bounded and avoids cross-request pagination races; Cloud must
obtain a binding ID from its separately authorized subscription path.

## Receipt readback

`POST /internal/operator-request-delivery/v1/receipts` additionally accepts
`prior_invocation_id`. Sprintctl derives the immutable key
`gateway:<prior_invocation_id>` and joins it to the delivery ledger only under
the current authenticated sender principal, workspace, repository, and
request owner. It returns at most the exact matching receipt: message ID,
owner delivery coordinates, envelope digest, and recorded time. It never
returns recipient principal/instance, admission JTI, grant/client details, or
message contents.

An empty receipt result is a snapshot only. A matching delivery transaction
may still be in flight and commit after that read. A null result is not proof
that no message will appear and does not authorize changing or reusing the
original logical key. The caller must retain the prior key and use the normal
owner delivery ledger retry/convergence path.

Historical readback intentionally does not require current recipient consent or
work freshness: it reports a previously committed receipt only. It requires a
fresh current sender identity and current request ownership. This makes an
uncertain exact attempt recoverable after a lost response or later binding
withdrawal without asserting present recipient authority or consumption.
Unknown database outcomes return a generic retryable response; no SQL or
connection details are exposed.

## Deployment gates

Enablement requires reviewed trusted Cloud service-caller authentication,
private ingress/DNS/network enforcement, exact gateway audience and key
registration, and Cloud response handling that keeps recipient provenance
inside the admission component. A normal user bearer or a caller-supplied
network flag is not the service-caller proof. This source does not supply or
claim any of these deployment facts.
