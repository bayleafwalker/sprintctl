# sprintctl state-protocol overlay

## Closed subjects

| Subject | State owner | Default depth | Primary anchors |
|---|---|---:|---|
| Claim ownership and handoff | SQLite or repository-scoped PostgreSQL claim rows | 2 | `sprintctl/db.py:create_claim`, `handoff_claim`; `sprintctl/pg.py` parity |
| Item status and dependencies | Backend work-item and dependency rows | 1 | status/dependency functions in `sprintctl/db.py` and `sprintctl/pg.py` |
| Event/history projections | Append-only event rows plus read surfaces | 1 | context, next-work, handoff, render builders |
| Sprint close boundary | Atomic sprint status/event write | 2 | `sprintctl/db.py`, `sprintctl/pg.py`: `close_sprint_with_boundary_event` |
| Work decisions and terminal status | Append-only `work_decision` rows plus the item's `terminal_decision_id`/`resolution` | 2 | `sprintctl/decisions.py`; `record_decision`/`_decide_locked` in `sprintctl/db.py` and `sprintctl/pg.py`; `sprintctl/authority.py:_handle_decision` |
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
- Explicit close commits `closed` and exactly one `sprint-close-boundary` event atomically; its database-local reference is `event:<id>` and depends on preserving the event/source mapping.
- A non-legacy item reaches `done` only with a bound terminal decision whose kind matches its `resolution`; `done` never transitions back, `legacy` never changes, and decisions are never updated or deleted, on both backends.
- `item.done` and a transition to `done` record an `accept` decision in the same transaction; carryover records a `supersede` decision naming the new item.
- Archive import demotes typed lifecycle events; trusted backend migration preserves their IDs and rejects authority-event ID remapping.
- Event-insert failure leaves the sprint active, and maintenance auto-close emits no capability boundary.

## Current limitation to preserve in reports

SQLite serializes claim creation with `BEGIN IMMEDIATE`. PostgreSQL serializes
claim admission per repository-scoped item by locking the authoritative
`work_item` row before checking and inserting claims. Independent-connection
histories establish the bounded invariant that two overlapping unrelated
exclusive claim attempts produce one acceptance and one rejection. Report this
as `concurrency-tested` application-invariant evidence, not as a general
cross-operation linearizability or fencing guarantee.

Document linkage is a workflow convention in this rollout, not an enforced claim gate. Do not claim the CLI blocks an undocumented or draft-governed item.

Sprintctl performs no LLM inference, ratification, or publication. A decision
records digests of its evidence and, once Releases exist, of its Release; the
evidence itself stays outside sprintctl.

## Verification environment

Use temporary SQLite databases and a disposable PostgreSQL repository/schema. Give each actor an independent connection, synchronize immediately before the conflict check or target write, record invocation/completion histories, and retain minimized traces. Never use the shared sprint backend for fault or concurrency tests.
