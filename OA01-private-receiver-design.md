# OA-01 private receiver source design

Status: isolated source candidate, default-off. No route is mounted, audience
minted, network policy changed, or owner schema migrated by this candidate.

## Decision

Use the existing gateway-signed end-user identity assertion as the caller
credential. The private receiver verifies the existing `X-Vuoro-Identity`
format with `GatewayAssertionIdentityResolver`, constructed for the exclusive
audience `sprintctl-operator-request-delivery/v1`. It requires a current
`work:evidence` authority, the configured workspace and repository, and a
valid short-lived assertion. The receiver does not accept a caller-built
`Identity`, principal, workspace, or repo grant.

Cloud's signed recipient-admission assertion remains a separate input. It
proves an owner-derived recipient eligibility snapshot; it does not
authenticate the sender or the HTTP caller. Sprintctl's existing private
`recipient_delivery_pg.deliver()` re-reads owner request, resolution, binding,
work, instance and capability state, verifies the admission assertion against
those rows, and writes the inbox message and delivery ledger in one owner
transaction.

The receiver contract is `operator-request-delivery/v1` at
`POST /internal/operator-request-delivery/v1`. The body separates the signed
gateway `invocation_id` from the durable OA request `request_id`, and contains
only repo, Cloud assertion and opaque owner coordinates. The signed invocation
ID must match both `X-Request-ID` and the parsed request body. JSON members are
unique, the body is read incrementally with a 32 KiB cap, and the audience must
be a single exact scalar. The route is excluded from OpenAPI and the router
registers no route unless explicitly enabled.

## Replay, expiry and uncertain results

The gateway resolver's JTI replay cache is process-local and is only an early
refusal. The receiver derives the owner idempotency key as
`gateway:<signed invocation_id>`. Sprintctl's existing append-only delivery
key ledger persists that key with the authenticated principal and owner
coordinates, so replays through a different resolver instance or connection
converge to the same message. Changed coordinates under that key conflict.
New gateway invocations for the same logical delivery converge through the
owner's logical delivery uniqueness constraint.

Gateway expiry is rechecked inside `deliver()` through the owner transaction.
Known typed domain refusals are returned as terminal `409 delivery-refused`.
Storage, transaction and commit failures are returned as
`503 delivery-outcome-unknown` with `retryable: true`; the receiver does not
claim rollback when commit state is uncertain. Retry requires a fresh signed
gateway invocation and owner idempotency convergence. No assertion token or
recipient secret is persisted by the receiver.

The connection provider must return an owned per-request lease. The receiver
requires it to start idle, owns one transaction, and leaves cleanup to that
lease. It never closes or rolls back a borrowed application connection.

## Enablement gates

This source candidate cannot be enabled in a deployment until the operators
have explicitly configured and verified all of the following:

1. The gateway issues this one dedicated audience with a unique invocation ID,
   bounded expiry, and the `work:evidence` authority only after the normal
   user/repo grant check.
2. The receiver uses the matching trusted public key, issuer, workspace,
   allowed repo set, key ID, and a startup `replay_not_before` boundary.
3. A trusted deployment-level private ingress guard is passed as process
   configuration. No caller header or body field is accepted as evidence of a
   private network path.
4. The router is mounted only on that private listener and remains absent
   from public MCP/operation catalogs.
5. Cloud has the pinned admission signing key configured and invokes the
   receiver using a gateway-signed end-user assertion. No service credential
   or mTLS identity is assumed by this design.
6. The operational read path can distinguish a saved delivery from an
   `outcome-unknown` response before any retry that changes request content.

Until those gates are evidenced, leave `enabled=False` and do not mount the
router. The source does not claim transport, deployment, field or production
qualification.

## Source and checks

Candidate base: Sprintctl `15892e7d79261433725ca20b7e5f427b0f22c1c4`.
Verifier dependency: `vuoro-service==0.1.104`, pinned by the existing
`gateway-test` extra to SHA256
`39f5ee328a8e4dd96f119c651d64e063811b3202d0ea621165f43231c51dc8e4`.

The candidate adds a Python 3.12 gateway-admission selection for receiver unit
checks and one PostgreSQL end-to-end case. The PG case signs both gateway and
Cloud admission assertions with ephemeral keys, calls the real owner delivery
transaction twice through separately constructed resolvers and independent
connections, and checks one message and one durable key row. It runs only in
the gateway-admission job, which installs the pinned verifier and a disposable
PostgreSQL 16 service.
