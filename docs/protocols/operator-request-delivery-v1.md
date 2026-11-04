# Request-resolution delivery: source design v1

Status: proposed OA-04/OA-06 design; not implemented or deployed. Existing
continuation reads and principal receipts do not establish native wake or
session consumption. This design adds no scheduler, effect authority, work
ownership claim, or independent work-to-workspace provenance. Selected
TypeSpec generation and estate foundation acceptance remain separate gates.

## Existing authority and scope

A request dependant stores work and a self-reported session; session strings
are not authenticated identities. The directory owns registered instances
under authenticated principals, and the inbox targets principals or instances.
Sprintctl sees the invocation caller but cannot check another principal's Cloud
grant. Cloud verifies the current bearer, then reads grant, membership,
workspace, role, epoch and repository set together before issuing a caller
assertion. This is as-of admission, not commit-time freshness.

V1 supports Cloud OAuth identity only. A subscriber with current `work:read`
and `work:evidence` explicitly consents to disclose its principal and instance
to the authorized requester for delivery. Its binding names the exact request,
item and its own active directory instance. The owner takes principal,
workspace, gateway assertion issuer, opaque subject, epoch, client ID, grant ID,
resource, the exact original OAuth token scope set, and the admitted effective `work:read`
ceiling from verified context, never subscriber arguments. Vuoro must first extend verified gateway identity with
issuer, subject and original token scopes as separately verified internal
fields, exactly bound to the verified principal, epoch and assertion; other
resolvers leave them absent and cannot use this path. The gateway assertion wire
contract must be versioned or compatibly extended before this binding can work. OAuth token issuer and gateway assertion issuer are
distinct. Missing verified fields, a non-Cloud identity or inactive/foreign
instance refuses. Grant replacement requires fresh binding; stored grant scopes
cannot expand the original admitted authority. Binding expresses interest, not
session or work ownership. Only its owner may withdraw it with CAS.

The binding is durable self-consent for routing, not an enduring bearer token
or an independent authorization grant. The original bearer may expire without
ending that consent; this does not renew its validity or authorize reads,
actions, or native wake. Never retain bearer or refresh secrets. A new delivery
requires a fresh live grant, membership, role and workspace check constrained by
the original scope and resource ceiling, plus exact principal, epoch, instance,
request and work checks. Revocation, withdrawal, epoch change, grant replacement
or loss of required original scopes refuses; restoration requiring a new grant
or identity requires a fresh binding. This check is not current-bearer admission.
Original OAuth scopes (including `vuoro:work.read` and `vuoro:evidence.record`)
and internal authorities (`work:read` and `work:evidence`) are distinct sets;
their relationship follows the verified gateway mapping and role intersection,
never a direct set comparison. The owner stores both provenance and admitted
ceiling without treating either as an authenticated caller-supplied argument.

Association is item-level: at least one active owner-recorded dependant row for
the item must exist; send checks status revisions of every active row for that
item, as the existing principal receipt does. Withdrawing one row leaves the
binding eligible only if another active row remains. Existing dependant
withdrawal is permitted only while the request is open; binding withdrawal is
separate and allowed after resolution. Session strings select no recipient.

## Recipient admission and write

A requesting principal with current `work:evidence` explicitly requests send
for one opaque binding ID and exact request, resolution and work status token.
It cannot supply recipient principal or grant fields. A private owner lookup
checks requester ownership and provides bound coordinates solely to the trusted
Cloud admission component. The requester sees binding IDs and generic failure
classes; after success the existing inbox sender/status surface may reveal the
opted-in recipient principal and instance. That disclosure is part of binding
consent. Grant, client, token and membership details are not disclosed.

The admission component checks the bound subject, gateway issuer, client, grant,
epoch, workspace, repository, original authority ceiling, live grant,
membership, role and workspace policy in one authoritative read, including the
requirement that every originally admitted token scope remains in the grant. Already
revoked or unavailable refuses. It issues a dedicated operation-bound assertion
through a private authenticated channel, never caller arguments. The owner
verifies configured issuer, distinct recipient-admission audience, signature
against a dedicated rotatable trust set, maximum 30-second lifetime,
issuance/expiry, unique ID, authority-read interval, exact caller/request/
resolution/work/binding and recipient coordinates, and original authority
ceiling. The JTI is accepted at most once for a new logical delivery; an exact
retry uses the owner ledger and cannot reuse an assertion for a new write.
Missing provenance, expiry, clock ambiguity, mismatched values or unavailable
verification fails closed. The conservative start of the authority-read interval, not signing time, is
the age bound; its end bounds the observation rather than claiming an exact
snapshot timestamp. At owner admission, the interval start must be at most 30 seconds old; signing delay cannot reset
that limit. The owner allows at most two seconds of future clock skew, refuses
older or further-future interval starts, and requires a new provider read after expiry.
Assertion issuance must not precede the recorded read beyond that skew.
The provider obtains an idle connection and starts a fresh read-only READ COMMITTED
transaction for that single eligibility statement; an already active transaction
is refused. Thus the snapshot cannot predate the recorded statement interval.

Revocation committed before the authoritative read begins must refuse; revocation racing
with or after it may race the owner commit. The response cannot claim recipient
authorization at commit or in the future. Recipients independently authenticate
with current grants for every inbox and owner read. Existing targeted inbox
reads require the exact instance ID; retired instances' messages remain stored.
No automatic retarget or wake occurs. An authorized successor registers and
binds its own instance and the requester explicitly sends for that new binding.
Until then the existing continuation-ready fallback remains available.

## Message, provenance and concurrency

A separate versioned notification message kind and receiver capability is
required. An ordinary `agent-message/v1` continue body is unsuitable: an
existing receiver could process forged text without verifying provenance. The
owner may send only when a current recipient declaration explicitly advertises
the new versioned notification capability; older or unknown receivers remain
unsupported and see continuation-ready instead. The new receiver contract must
require an owner verification read before treating a notification as an owner
resolution, fail closed on unavailable or mismatched provenance, and never turn
notification into authority to run an effect. Its envelope contains only exact
request, resolution, work and opaque binding coordinates, no request procedure,
evidence payload or other dependants' sessions. It must use a versioned owner
message schema and cannot broaden existing v1 parsing.

The restricted verification read is principal-authorized and instance-addressed:
the recipient supplies an instance ID and the owner verifies it belongs to the
current principal. Any session of that same principal can present that ID; it is
not authenticated instance identity or proof of runtime consumption. The read
checks an immutable delivery record binding message ID and envelope digest to
the request, resolution, work, binding and recipient. Without that record the
message is unverified. Existing broader request reads and inbox status remain
unchanged.
The immutable delivery key is repository, workspace, request, resolution
revision, work item, status token, instance, opaque binding ID and binding revision. Concurrent
attempts for the same logical key with different idempotency keys converge to
one message; differing tuples represent distinct deliveries. The transaction
locks request `FOR UPDATE`, binding `FOR UPDATE`, instance `FOR SHARE`, then work
`FOR SHARE`; the request lock serializes dependant mutations. It verifies exact
current resolution, active binding and dependants, all stored status tokens,
work status, instance, request freshness and the owner event predicate
`resolved-human-attestation` with `completion_basis=human-attestation` and
`protected_effect_authorized=false` at owner
transaction time; then atomically inserts inbox message and delivery record. It
uses the existing per-workspace/repository inbox counter lock and sequence
allocation, preserving commit-ordered cursor visibility against ordinary sends. Bind/unbind take request
then binding locks; retirement takes its existing instance lock. Tests must
validate ordering against all participating operations. Deadlock or
serialization errors are explicit retryable unknowns, never success. The
restricted verification read uses a consistent snapshot and returns unavailable
after binding withdrawal or instance retirement. Verification never proves that
the named instance or any runtime consumed the message.

Same sender/idempotency key and exact payload can replay a historical result
after lost response only after current sender authentication, authority and
requester ownership are rechecked. It never asserts current recipient authority,
work freshness or consumption. Changed payload under one key conflicts. New
logical deliveries require new recipient admission. Message ID and digest are
generated once inside the successful transaction and stored with the unique
key; retries return that historical ID without re-creating a message. The normal
ledger must not return prior output before sender checks. Recipient admission
and work status are not rechecked on historical replay, which is labeled.

Status-derived tokens cannot detect ABA status changes or description edits.
No material-currentness claim is made; a stronger owner revision is separately
required. No request, work or message identifier authorizes an effect.

## Required falsifiers and implementation boundary

Qualify actual cross-service admission and owner transactions: legitimate
subscription and two recipients; foreign principal/workspace/repository,
client/grant/issuer substitution and sender-supplied assertions; missing scopes,
retired instance, revoked grant before admission and revocation racing after it;
rebind to the same instance with a new binding ID, withdrawn binding and
historical replay; wrong recipient or digest; forged ordinary
message; multiple dependant rows with one or all withdrawn; changed status,
expired freshness, ABA and description edits; identical and changed-key retries;
concurrent delivery, ordinary send and polling, retirement and withdrawal;
unsupported or malformed resolution events including each missing/mismatched
attestation field; delayed signing and delayed owner admission beyond the
authority-read age bound; unsupported receiver capability;
no message on refusal. Tests
must observe commits, counts and restricted reads with separate connections,
preserve unknown interrupted outcomes and demonstrate the as-of boundary.
Migrations separately prove catalog identity, compatibility, runtime privileges
and rollback. No live grant, data, service or schema change follows from this
proposal.
