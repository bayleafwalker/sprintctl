# Retired claim archive boundary

Legacy `claim` records are retained only in `claim_history` for audit,
export/import, and recovery evidence. They are not authority state.

The archive must never be used to:

- establish ownership or credentials;
- recover a token or lease;
- decide dispatch, item status, or sprint transitions; or
- recreate a live claim.

Live coordination is represented exclusively by the reservation ledger. A
reservation is session-bound, advisory, and may be released, reassigned, or
interrupted after seven days of inactivity. It carries no bearer credential.

Migration policy:

1. archive every legacy claim row idempotently by its stable history identity;
2. preserve `claim_history` in transfer and recovery snapshots;
3. remove the live claim table and all claim-proof runtime APIs only after the
   archive/recovery proof is green for SQLite and PostgreSQL.

Rollback of a deployment migration restores the prior compatible application
artifact against the retained archive; it must not turn historical rows into
live authority state.
