# sprintctl state-protocol overlay

## Closed subjects

| Subject | State owner | Default depth | Primary anchors |
|---|---|---:|---|
| Claim ownership and handoff | SQLite or repository-scoped PostgreSQL claim rows | 2 | `sprintctl/db.py:create_claim`, `handoff_claim`; `sprintctl/pg.py` parity |
| Item status and dependencies | Backend work-item and dependency rows | 1 | status/dependency functions in `sprintctl/db.py` and `sprintctl/pg.py` |
| Event/history projections | Append-only event rows plus read surfaces | 1 | context, next-work, handoff, render builders |
| Local recovery tokens | Local filesystem projection of claim proof | 1 | claim recovery helpers and CLI commands |
| Document-linked work | sprintctl refs plus immutable repository documents | 1 | ref CRUD, item/resume surfaces, `docs/reference/doc-refs.md` |
| Backend parity | SQLite and PostgreSQL implementations | 2 | `sprintctl/db.py`, `sprintctl/pg.py`, PostgreSQL integration tests |

Escalate to Depth 3 for lease/fencing redesign, irreversible multi-object transitions, remote worker delegation, or semantics that cannot be covered by bounded independent-connection histories.

## Required scenarios

- Two independent actors create exclusive claims for one item concurrently on each backend.
- Coordinator delegation competes with an unrelated exclusive claim.
- Handoff rotates proof while the old owner attempts heartbeat, release, and status mutation.
- Response loss occurs after claim create or handoff commit.
- Expiry and reassignment occur at the boundary of backend time.
- SQLite and PostgreSQL return equivalent accepted/rejected histories and public contract shapes.
- A shaped item resolves to one immutable governing document revision or an explicit `no-doc:` decision.
- Resume and close reconciliation detect missing, mutable, superseded, or revision-mismatched doc refs.

## Current limitation to preserve in reports

SQLite serializes claim creation with `BEGIN IMMEDIATE`. PostgreSQL currently performs a conflict check followed by insert without a documented database constraint that closes the concurrent check-then-write window. Do not report cross-backend exclusive-claim linearizability until an independent-connection history establishes it or the product is repaired under separate authorization.

Document linkage is a workflow convention in this rollout, not an enforced claim gate. Do not claim the CLI blocks an undocumented or draft-governed item.

## Verification environment

Use temporary SQLite databases and a disposable PostgreSQL repository/schema. Give each actor an independent connection, synchronize immediately before the conflict check or target write, record invocation/completion histories, and retain minimized traces. Never use the shared sprint backend for fault or concurrency tests.
