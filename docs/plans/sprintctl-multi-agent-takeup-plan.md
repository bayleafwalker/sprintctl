# sprintctl multi-agent takeup plan

Workstream A of `/projects/dev/agentops/docs/plans/agentops/agent-ops-substrate-plan.md`. Sqlite-only. Adds a sprint-level "takeup" event-pair, removes any single-active-sprint assumption that survives in render/show paths, and surfaces takeup state in the per-sprint render. This plan is the prerequisite for everything else in the substrate plan; the event model defined here is the contract that workstream B's pg schema must mirror exactly.

## Goal

Sprintctl gains a sprint-level takeup signal — an opaque `taken_up` / `released` event-pair — distinct from item-level claims, with no TTL or heartbeat semantics, supporting concurrent takers and crash recovery via `--force`.

## Scope

What changes in this workstream:

- New CLI verbs: `sprintctl takeup` (group) with subcommands `take`, `release`, `list`, `show`.
- New event types written into the existing `event` table: `sprint-taken-up`, `sprint-released`.
- New canonicalizers in `contracts.py` for the two event types.
- New helper queries in `db.py` for "current takeup state of sprint" and "takeup history of sprint".
- New index on `event(sprint_id, event_type, created_at)` to keep the takeup-status query bounded.
- Multi-active sprint allowance: replace single-result `get_active_sprint` callers in render/show paths with a multi-active aware variant; document that `kind=active_sprint AND status=active` may have N>=0 rows and is not a uniqueness invariant.
- Render changes: `sprintctl render` and `sprintctl sprint show --detail` expose current takeup state alongside existing claim/health output.
- New `sprintctl sprint list --active` shorthand to enumerate the N active sprints (cockpit precursor).
- Test additions: `tests/test_takeup.py` covering happy-path pair, multi-taker, force, idempotent release, ordering; `tests/test_multi_active.py` covering N>1 active sprints; render snapshot updates.
- Doc additions: `docs/advanced/takeup.md`, updates to `docs/advanced/coordinator-mode.md` distinguishing takeup from claims, an entry in the agent protocol output.

What does NOT change in this workstream:

- No changes to claim semantics, claim TTL, heartbeat, handoff, or coordinator mode.
- No changes to existing event types or payload contracts.
- No backend abstraction. All work lands in the existing sqlite path.
- No daemon, no service, no dispatch logic.

## Schema changes

No new tables. Two additions only.

### New event_type values

The `event.event_type` column is already free-text (no CHECK constraint). The new values become part of the documented contract:

- `sprint-taken-up`
- `sprint-released`

Both are recorded with `source_type='actor'` by default (override allowed), `work_item_id=NULL` (sprint-level, never tied to an item).

### Payload shape — `sprint-taken-up`

```json
{
  "summary": "<short human description, default: 'sprint takeup'>",
  "detail": "<optional longer string or null>",
  "tags": ["takeup"],
  "actor_kind": "agent" | "human",
  "hostname": "<gethostname()>",
  "pid": <int>,
  "instance_id": "<uuid or env SPRINTCTL_INSTANCE_ID>",
  "runtime_session_id": "<env SPRINTCTL_RUNTIME_SESSION_ID or CODEX_THREAD_ID, may be null>",
  "context": "<optional free-form context string, may be null>",
  "forced": <bool, true iff --force was used>
}
```

`actor` (the top-level event column) carries the agent or human id. `created_at` is the `taken_up` timestamp; no separate `ts` field in payload.

### Payload shape — `sprint-released`

```json
{
  "summary": "<short human description, default: 'sprint release'>",
  "detail": "<optional longer string or null>",
  "tags": ["takeup"],
  "actor_kind": "agent" | "human",
  "hostname": "<gethostname()>",
  "pid": <int>,
  "instance_id": "<uuid or env>",
  "runtime_session_id": "<env or null>",
  "reason": "<optional free-form reason, may be null>",
  "matched_takeup_event_id": <int or null>
}
```

`matched_takeup_event_id` is best-effort: the most recent un-released `sprint-taken-up` whose (`actor`, `instance_id`) match this release. Null if none was found (release without prior takeup is allowed and recorded; this surfaces as a soft warning in CLI output).

### New index

```sql
CREATE INDEX IF NOT EXISTS idx_event_sprint_type_ts
    ON event(sprint_id, event_type, created_at);
```

Added as migration 9. Bounds the "current takeup state" query (scan only takeup events for one sprint, newest first per actor+instance).

### Canonicalizers

`contracts.canonicalize_event_payload` extends with two branches:

- `sprint-taken-up` → `canonicalize_sprint_taken_up_payload(payload)` (defaults `tags=["takeup"]`, normalizes booleans, stringifies optional fields).
- `sprint-released` → `canonicalize_sprint_released_payload(payload)` (same shape discipline).

Workstream B's pg schema reads the same canonical payloads; no shape divergence allowed.

## New CLI surface

New top-level group `takeup`. Mirrors the noun/verb pattern used by `claim`.

### `sprintctl takeup take`

Records a `sprint-taken-up` event for the named sprint and the current actor.

Flags:

- `--sprint-id INT` (required)
- `--actor TEXT` (required) — agent or human identifier
- `--actor-kind {agent,human}` (default: `agent`)
- `--context TEXT` (optional) — free-form context string for the takeup record
- `--instance-id TEXT` (optional, defaults to `SPRINTCTL_INSTANCE_ID` or fresh uuid)
- `--runtime-session-id TEXT` (optional, defaults to `SPRINTCTL_RUNTIME_SESSION_ID` or `CODEX_THREAD_ID`)
- `--hostname TEXT` (optional, defaults to `socket.gethostname()`)
- `--pid INT` (optional, defaults to `os.getpid()`)
- `--summary TEXT` (optional, default: `"sprint takeup"`)
- `--detail TEXT` (optional)
- `--force` — emit a `sprint-taken-up` even if a prior un-released takeup exists for the same `(actor, instance_id)` pair. Sets `forced=true` in payload. Does not delete or modify the prior event; it is purely additive (events are append-only).
- `--json` — machine-readable output

Default behavior (without `--force`): if a `sprint-taken-up` event exists for `(actor, instance_id, sprint_id)` with no matching `sprint-released` after it, the command exits with status 2 and message `Sprint #N already taken up by actor='X' instance='Y'. Use --force for crash recovery.` This is the only "uniqueness" check; multiple distinct actors/instances may take up the same sprint without conflict.

Output (text): `Sprint #N taken up by <actor> (instance: <instance_id>, host: <hostname>) event #<eid>`

Output (JSON):

```json
{
  "operation": "takeup_take",
  "event_id": <int>,
  "sprint_id": <int>,
  "actor": "<text>",
  "actor_kind": "agent|human",
  "instance_id": "<text>",
  "hostname": "<text>",
  "pid": <int>,
  "forced": <bool>,
  "context": "<text|null>"
}
```

Error cases:

- Sprint not found → exit 1, `Sprint #N not found.`
- Already-taken-up by same `(actor, instance_id)` without `--force` → exit 2, message above.
- Sprint `kind != 'active_sprint'` → soft warning on stderr, takeup still recorded (operator may legitimately work a backlog/archive sprint; takeup is a signal, not a permission).

### `sprintctl takeup release`

Records a `sprint-released` event.

Flags:

- `--sprint-id INT` (required)
- `--actor TEXT` (required)
- `--instance-id TEXT` (optional; if omitted, attempts to match the most recent un-released takeup by `actor` only)
- `--reason TEXT` (optional)
- `--summary TEXT` (default: `"sprint release"`)
- `--hostname`, `--pid`, `--runtime-session-id`, `--actor-kind` (same defaults as `take`)
- `--json`

Behavior: scans events for the sprint to find the most recent `sprint-taken-up` for `(actor, instance_id)` not yet matched by a `sprint-released`; sets `matched_takeup_event_id` accordingly. If none found, the release is still recorded with `matched_takeup_event_id=null` and a stderr warning `No matching takeup found; recording release anyway.` (exit 0). Releases are always permitted — no `--force` flag, no error path.

Output (text): `Sprint #N released by <actor> (matched takeup #<eid> | no prior takeup) event #<eid>`

### `sprintctl takeup list`

Lists current takeup state for one or all sprints.

Flags:

- `--sprint-id INT` (optional; if omitted, lists across all sprints with at least one takeup event)
- `--all-history` (default off) — include released takeups in output
- `--json`

State derivation: for each `(sprint_id, actor, instance_id)` triple, determine the "current" state by walking events in `created_at` order — last `sprint-taken-up` with no following `sprint-released` is "active", else "released". Output groups by sprint then by active-first.

Text output (one row per active takeup, optional released-history table after):

```
SPRINT  ACTOR        INSTANCE          HOST       SINCE                 CONTEXT
#7      claude-a     7f3a-…            devbox     2026-04-26T14:02:11Z  cockpit-realign
#7      semper       4422-…            workstn-1  2026-04-26T15:45:08Z  -
#11     codex-b      a18d-…            devbox     2026-04-26T08:11:00Z  audit-ledger
```

JSON output:

```json
{
  "operation": "takeup_list",
  "active_takeups": [
    {
      "sprint_id": <int>,
      "actor": "<text>",
      "actor_kind": "agent|human",
      "instance_id": "<text>",
      "hostname": "<text>",
      "pid": <int>,
      "taken_up_at": "<iso>",
      "taken_up_event_id": <int>,
      "context": "<text|null>",
      "forced": <bool>
    }
  ],
  "released_takeups": [ ... same shape plus released_at, released_event_id, reason ... ]
}
```

### `sprintctl takeup show`

Convenience: full takeup history for one sprint, paired up. Used by render and by humans investigating "who's been on sprint #N".

Flags:

- `--sprint-id INT` (required)
- `--json`

Output is a chronologically ordered list of (`taken_up`, `released?`) pairs with timestamps.

## Multi-active sprint decision

The schema does NOT enforce single-active. `sprint.status` has only the column-level CHECK `('active', 'closed', 'planned')`; no UNIQUE on status.

Where single-active leaks in:

- `db.get_active_sprint(conn)` — returns one row (most recent created_at) where `status='active' AND kind='active_sprint'`. Used as a default by many CLI verbs (`render`, `sprint show`, `handoff`, `next-work`, `session resume`, `usage --context`, `claim list-sprint`).

The fix is non-destructive: add `db.list_active_sprints(conn)` returning all matching rows, and treat `get_active_sprint` as "the implicit default sprint when only one is active". Verbs that take `--sprint-id INT` keep working unchanged. Where `--sprint-id` is omitted and `len(list_active_sprints()) > 1`, the verb errors with:

```
Multiple active sprints (#7, #11). Pass --sprint-id explicitly.
```

This is the correct behavior — the cockpit needs N active sprints, but operator commands without an explicit id should not silently pick one.

Concretely:

- `db.get_active_sprint` keeps its current signature for back-compat but its docstring is updated to "returns the single active sprint, or the most recent if multiple exist; callers wanting strictness must use `list_active_sprints`".
- New `db.list_active_sprints(conn) -> list[dict]` ordered by `created_at DESC`.
- A new helper `_resolve_implicit_sprint(conn)` in `cli.py` centralizes the "default to active, error if ambiguous" rule and replaces ad-hoc `get_active_sprint()` calls in `render_cmd`, `sprint show`, `handoff`, `next-work`, `session resume`, `usage --context`, `claim list-sprint`. Each of those callers gets the new error path and a test.

No migration is needed for multi-active; it is a CLI/behavioral change only.

## Render changes

`sprintctl render` and `sprintctl sprint show --detail` both grow a takeup section.

### `render`

`render.render_sprint_doc` gets a new optional kwarg `active_takeups: list[dict] | None`. When non-empty, a section is emitted between the header and the first track:

```
Takeup: 2 active
  - claude-a@devbox     (instance 7f3a-…)  since 2026-04-26T14:02:11Z   ctx: cockpit-realign
  - semper@workstn-1    (instance 4422-…)  since 2026-04-26T15:45:08Z
```

When zero active takeups, the section is omitted (keeps the existing render shape for non-multi-agent repos).

`cli.render_cmd` collects the takeups via the new `db.list_active_takeups(conn, sprint_id)` query and passes them in.

### `sprint show --detail`

`_collect_sprint_show_payload` adds a `takeup` block under `detail`:

```json
"detail": {
  "risk": { ... },
  "stale_count": <int>,
  "track_health": { ... },
  "takeup": {
    "active_count": <int>,
    "active": [ ... same shape as takeup list active rows ... ]
  }
}
```

`_emit_sprint_show_text` prints a one-line summary (`Takeup: N active (claude-a, semper)`), followed by a per-actor line under the existing `Track health` section.

### `sprint list --active`

A shorthand: `sprintctl sprint list --active` filters to `status=active AND kind=active_sprint` and prints the same table as today. Useful for cockpit precursor and operator scripting. Pure CLI sugar; no schema work.

## Test plan

New tests live under `tests/`. All use the existing `conftest.py` in-memory sqlite fixture.

### `tests/test_takeup.py`

- **happy path**: create sprint, take it up, assert `takeup list` shows one active row, release, assert `takeup list` shows zero active and one released-history row.
- **multi-taker**: two distinct `(actor, instance_id)` pairs take up the same sprint concurrently; both appear in `list`; releasing one leaves the other active.
- **same-actor different-instance**: same `actor` with two distinct `instance_id` values is allowed without `--force`.
- **same-actor same-instance without force**: second `take` returns exit 2 with the conflict message.
- **same-actor same-instance with --force**: second `take` succeeds, payload `forced=true`, `list` shows two events, two active rows (operator must release each).
- **release without prior takeup**: release succeeds, stderr warning emitted, `matched_takeup_event_id=null`.
- **release matches most recent unreleased**: with two takeups by same `(actor, instance)` (one forced), release pairs with the newer event_id.
- **canonicalization**: payload survives round-trip through `contracts.canonicalize_event_payload` for both event types; missing fields receive defaults.
- **index presence**: after `init_db`, `idx_event_sprint_type_ts` exists.
- **CLI JSON contract**: `takeup take --json`, `release --json`, `list --json`, `show --json` all match the documented shape.

### `tests/test_multi_active.py`

- **two active sprints**: create two `active_sprint` sprints, set both to `active`; `list_active_sprints` returns both; `render_cmd` without `--sprint-id` errors with the multi-active message; `render_cmd --sprint-id 7` works.
- **affected verbs**: each of `render`, `sprint show`, `handoff`, `next-work`, `session resume`, `usage --context`, `claim list-sprint` follows the same default/error rule.
- **single active still implicit**: with one active sprint, all verbs continue to default to it (back-compat).

### Render snapshot

Update `tests/test_cli_output_format.py` (or add a new module) to assert:

- Render with zero takeups matches the existing snapshot byte-for-byte (no regression).
- Render with N takeups includes the new section in the documented order.

### Touch-ups

- `tests/test_event_payload_contracts.py` extended for the two new canonicalizers.
- `tests/test_docs_integrity.py` extended to verify the new `takeup` agent-protocol entry is present and the `docs/advanced/takeup.md` link is reachable.

## Out of scope

- **Heartbeat / TTL on takeup.** Sprintctl never derives liveness from takeup events. Session liveness is workstream C (actionq's domain).
- **Pg backend.** Workstream B will add it. This plan is sqlite-only; the event model defined here is the wire-stable contract Workstream B's pg schema must mirror.
- **Session lifecycle integration.** Actionq-daemon will issue takeup/release as side effects of session start/stop in workstream C. This plan only delivers the CLI surface and event model.
- **Cockpit reads.** Workstream E queries this data via pg (after workstream B). Nothing in workstream A talks to a frontend.
- **Cross-repo aggregation.** Single-host, single-db sqlite. The "all active sprints across all repos" view is workstream B+E.
- **Auditctl emission.** Sprintctl-to-auditctl bridging is workstream D's call to add; this plan does not assume auditctl exists.
- **Backend mode flag (`SPRINTCTL_BACKEND`).** Introduced in workstream B. This workstream stays on the existing implicit-sqlite path.

## Implementation order

Each step is independently shippable. Stop after any of them and the system is coherent.

1. **Schema migration 9 + canonicalizers + db helpers.** Add `idx_event_sprint_type_ts`, the two `canonicalize_*_payload` functions, and `db.list_active_takeups`, `db.list_takeup_history`, `db.list_active_sprints`. Tests at the db layer. No CLI surface yet — purely additive.

2. **`sprintctl takeup take` + `release`.** The two write verbs. Includes `--force` from day one. Includes the conflict-detection query. Tests: happy path, multi-taker, same-instance conflict, force, release matching, release without takeup. Shippable: agents and humans can mark themselves on a sprint.

3. **`sprintctl takeup list` + `show`.** Read verbs. Tests: list across sprints, single-sprint history, JSON shape. Shippable: operator can introspect.

4. **Multi-active unlock.** Add `_resolve_implicit_sprint`, propagate to all affected verbs, update tests. Add `sprint list --active` shorthand. Shippable: cockpit precursor — multiple active sprints can coexist without ambiguous CLI behavior.

5. **Render takeup section + `sprint show --detail` takeup block.** Wires the read queries into the existing render paths. Snapshot tests updated. Shippable: per-sprint output now reflects takeup state, which is what cockpit will eventually consume via pg.

6. **Docs.** `docs/advanced/takeup.md` (concept, when to use, how it differs from claims, force semantics), update `docs/advanced/coordinator-mode.md` to point at the new doc, add agent-protocol entry. Update `docs/plans/roadmap-reset.md` if needed to acknowledge the new track. Snapshot the closing sprint into `docs/sprint-snapshots/` once the workstream is closed.

Steps 1–3 deliver the takeup primitive. Step 4 unlocks the multi-active assumption that cockpit needs. Step 5 makes the new state visible to existing operator tools. Step 6 closes the workstream and hands the baton to workstream B.

## Risks and mitigations

- **Existing event_type free-text means typos are easy.** Mitigation: the two new types are referenced from a module-level constant in `db.py` (`SPRINT_TAKEN_UP_EVENT = "sprint-taken-up"`, `SPRINT_RELEASED_EVENT = "sprint-released"`), and the canonicalizers branch on that constant. CLI verbs use the constants directly.
- **Multi-active behavioral change could surprise existing operators.** Mitigation: with one active sprint (the common case), every verb behaves identically. The new error path only triggers when N>1 active, which is itself new behavior the operator just enabled. Document this in the takeup doc.
- **Release-without-takeup is a soft warning, not an error.** Mitigation: the warning is on stderr, exit code is 0, and the event is recorded. This matches the "events are append-only and opaque" stance — no enforcement, just facts.
- **`--force` could mask real bugs (sessions that crashed cleanly should release, not be force-retaken).** Mitigation: documented as crash recovery only, with `forced=true` always present in the payload so the cockpit and audit log can flag it.
- **Workstream B must not diverge on payload shape.** Mitigation: the canonicalizers in `contracts.py` are the single source of truth. Workstream B's pg schema imports the same constants and asserts payload shape on insert. A `tests/test_takeup.py` round-trip through canonicalize is the contract test.

## Critical files for implementation

- `/projects/dev/sprintctl/sprintctl/db.py` — migrations, new db helpers
- `/projects/dev/sprintctl/sprintctl/cli.py` — new takeup group, `_resolve_implicit_sprint`, affected verbs
- `/projects/dev/sprintctl/sprintctl/contracts.py` — two new canonicalizers
- `/projects/dev/sprintctl/sprintctl/render.py` — takeup section in render output
- `/projects/dev/sprintctl/tests/test_takeup.py` (new)
- `/projects/dev/sprintctl/tests/test_multi_active.py` (new)
