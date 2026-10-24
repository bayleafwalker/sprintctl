# Operator request lifecycle candidate

This document describes the schema 28 source candidate. It is not a deployed
service or a qualified human workflow.

## Records and scope

`operator_request` stores the stable request ID, requester, current revision,
material packet revision, lifecycle state, complete packet and digests.
`operator_request_dependant` links one work item and source session to the
request, retaining the status revision observed when it was attached.
`operator_request_event` records revisioned progress and lifecycle history.
Every read and write binds the repository to the service scope and workspace to
the authenticated identity; neither is accepted as a caller argument.

Request text is untrusted instruction content. The packet validator refuses
credential and transcript fields by name, requires a complete versioned
procedure, and rejects duplicate dependencies and unsafe integer values.
Mutations use the owner idempotency ledger and expected revisions. An exact
retry returns the stored result; a stale revision returns a conflict.

## State changes

- Publish creates an `open` request and its initial active dependants.
- Procedure progress appends a step event for one active work/session link.
- A performed report moves the request to `reported-performed`; partial or
  uncertain reports move it to `verifying`. None of those reports resolve the
  request.
- `work.request.attest-v1` requires `work:approve`, a current revision and a
  prior performed or uncertain report. It records a labeled
  `human-attestation` event and resolves the request. It does not grant
  protected effect authority or replay an operation.
- Decline requires `work:approve` and remains distinct from completion.
- Withdrawing one dependant keeps the others active; withdrawing the last one
  moves the request to `withdrawn`.
- Supersession creates a replacement request with the active dependants and
  marks the old request superseded. Its old dependant rows remain as withdrawn
  history.

The event table rejects update, delete and truncate in PostgreSQL. Startup
checks the exact catalog including the event guard function and triggers.

## Known limits

Freshness currently compares the packet's `fresh_until` value with the owner
clock. There is no live session-liveness observation or source-owner
revalidation, so this does not prove a blocker is still current. The `unknown`
freshness value is reserved for that future evidence path.

This candidate does not yet provide source-observation verification, per-
dependant continuation acknowledgements, native wake/resume, local offline
outbox reconciliation, assignment or defer-until conditions. Event reads let
clients reconcile durable owner history; they do not prove that an event was
delivered to a native runtime.
