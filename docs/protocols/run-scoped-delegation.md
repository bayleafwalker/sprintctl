# Run-scoped delegated credentials (owner schema 27)

Sprintctl owns the delegation record and validates it at each operation. The
Vuoro resolver may call the internal token resolver, then must construct an
immutable `sprintctl.delegation.DelegatedRunContext` from the returned owner
record. It must not derive run scope or operations from caller-supplied fields.
The internal resolver is not a public catalog operation.

The public owner surface is:

- `work.run.delegate-v1`: mint a credential for one caller-owned run, one
  registered creator instance, and a non-empty subset of the four-operation
  allowlist. TTL is an integer (booleans are rejected) from 1 through 14,400
  seconds. Expiry is bounded by the owner clock and by a trusted parent
  `expires_at` if the identity has one. Authority snapshots cannot widen the
  parent grant. A delegated identity cannot delegate again.
- `work.run.delegation-get-v1` and
  `work.run.delegation-revoke-v1`: operate only when the caller's exact
  principal, workspace, client, and grant match the stored parent binding.

The allowlist is exactly `work.run.resolve-v1`,
`work.run.predecessor-context-v1`, `work.session-note.write-v1`, and
`work.evidence.append-v1`. Every operation is tied to the exact run. Reads
recheck the owner row; writes hold a shared delegation-row lock through their
transaction, while revocation takes an exclusive row lock. Thus a write
already in flight may commit before revocation, but no delegated operation
can commit after revocation commits.

The bearer token contains 256 random bits and is returned only in the first
successful mint response. Sprintctl persists only its SHA-256 digest. The
idempotency ledger contains the redacted owner result, never the token. A
same-key retry will not return the token; if the first reply was lost, inspect
and revoke the record, then mint with a new key.

An explicit `result-linked` receipt for the run and retirement of the exact
creator instance revoke matching delegations in the same owner transaction.
Settlement is deliberately not inferred from session notes, which have no
terminal-result semantics. There is no automatic revocation on such a note.

Resolver shape:

```python
resolved = sprintctl.delegation_pg.resolve_token(store, bearer_token)
# resolved.delegation: DelegatedRunContext(delegation_id, run_id, operations)
# resolved.{repo_id, principal_id, workspace_id, client_id, grant_id,
#           authorities, expires_at}: owner-derived binding and bounds
```

The service should create a narrowed identity with this returned parent
binding, owner-authority snapshot, `delegation=resolved.delegation`, and
`expires_at=resolved.expires_at`. The service must still resolve the token at
authentication time. Sprintctl then revalidates the delegation row inside its
read or write transaction.

The bearer is a copyable 256-bit secret: the owner does not attest which
process or device presents it. The creator instance binds mint authority and
retirement revocation, but is not a hardware or runtime attestation. When a
parent identity has no trusted `expires_at`, the owner can only cap the child
by its own clock and the requested four-hour maximum; the record does not
inherit an external parent lifetime that was never supplied.

Schema 27 is an additive PostgreSQL migration. This source change does not
execute a production migration or deploy a runtime.
