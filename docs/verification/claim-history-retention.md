---
doc_id: sprintctl.claim-history-retention-completion
status: verified
checked: 2026-07-19
---

# Claim history retention completion note

Remote claim expiry now supersedes rather than deletes history. PostgreSQL
maintenance updates expired active rows to `status='expired'`; reacquisition
also marks elapsed rows before inserting the replacement. SQLite carries the
status column with the same default but retains its prior operational behavior.

## Claim-query audit

Audit command:

```text
rg -n -U "FROM claim|JOIN claim|UPDATE claim|DELETE FROM claim|INSERT INTO claim" sprintctl --glob '*.py'
```

Results reviewed:

- Active conflict/projection lookups in `db.py`, `pg.py`, and `authority.py`
  filter `status='active'` as well as backend-time expiry.
- Identity and status-by-ID lookups intentionally return retained expired rows.
- History/export lookups intentionally include every status.
- Credential-collision lookups intentionally include retained rows because a
  historical secret must not be reissued.
- Proof-bearing release remains a deliberate delete for active claims; revoke
  and release-history redesign are outside this change.
- Remote maintenance is the only expiry sweep and now updates status. SQLite's
  existing delete sweep is retained by the schema-parity constraint.

## Acceptance evidence

- PostgreSQL integration coverage exercises expiry and reacquisition and
  asserts that both rows remain with statuses `expired, active`.
- The additive migration is exercised against a copy of repository state; the
  pre-migration copy contained 21 sprints, 49 tracks, 173 work items, 381
  events, 105 refs, 33 dependencies, and no live claim rows. Migration advanced
  schema version 11 to 12 without changing any row count; `status` was added
  `NOT NULL DEFAULT 'active'`.
