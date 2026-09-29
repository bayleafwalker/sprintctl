---
doc_id: sprintctl.lease-epoch-schema-completion
status: verified
checked: 2026-07-19
---

# Lease epoch schema completion note

> **Superseded.** This note describes the live `claim` table as of 2026-07-19.
> That table no longer exists: SQLite migrations 19–20 (`_migration_19`,
> `_migration_20` in `sprintctl/db.py`) and PostgreSQL schema versions 9–10
> (`_apply_schema_version_9`, `_apply_schema_version_10` in `sprintctl/pg.py`)
> archived every row into the read-only `claim_history` table, dropped `claim`,
> and nulled `claim_token` (commit `9e99625`, "drop the live claim relation").
> Kept as historical verification evidence; see
> [docs/protocols/reservation-model.md](../protocols/reservation-model.md) for
> the current model.

`lease_epoch` is an additive claim column with `NOT NULL DEFAULT 1`. Remote
token rotation increments the current row. Remote reacquisition first retains
elapsed claims as `expired`, then inserts the next row at one greater than the
maximum expired epoch for the item. Claim JSON and text status surfaces expose
the value. There is no `expected_epoch` input or fencing enforcement.

PostgreSQL integration coverage starts at epoch 1, rotates to 2, expires and
reacquires at 3, and asserts both history rows remain. The disposable
PostgreSQL URL is not configured on this host, so that checked-in integration
history is skipped here; schema/CLI tests run locally. SQLite schema version 13
adds the parity column without changing local purge, rotation, or reacquisition
behavior.
