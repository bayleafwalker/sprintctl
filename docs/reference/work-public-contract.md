# work.public.* — the public-work contract (v1)

Owner: sprintctl. Consumer: the vuoro MCP edge (`list_ready_work`,
`describe_work`), served on the vuoro.cloud gateway. Tracker: agentops#2514.
Design: agentops `docs/design/e1/e1-stronger-baseline-design-2026-09-22.md`
section 5.

These two operations are the only sprintctl reads the E1 surface may call.
They are named `work.public.list/v1` and `work.public.item/v1` in the design;
the adapter kit forbids `/` in an operation name, so they register as
`work.public.list-v1` and `work.public.item-v1`. A v2 (adding `objective` and
`acceptance` as deliberately authored columns, sequenced with E2) registers
beside v1, not over it.

## Operations

| operation | input | authority | semantics |
|---|---|---|---|
| `work.public.list-v1` | `{}` | `work:read` | read |
| `work.public.item-v1` | `{work_id: integer >= 1}` | `work:read` | read |

Workspace scope comes from the identity assertion (`context.repo_id`), never
from the request body.

## Envelope

```
{ authority: "sprintctl", as_of: <UTC ISO 8601, Z>, state: "ok", items: [...] }
{ authority: "sprintctl", as_of, state: "ok", item: {...} }
```

`state` is `ok | unavailable` in the schema so a consumer knows the
vocabulary, but sprintctl never emits `unavailable` with a payload. An
unavailable authority is a rejection (`postgres-runtime-unavailable`, 503),
which the edge must surface as a tool error — never as an empty list.

## Records

List item (all fields required, `additionalProperties: false`):

| field | type | source |
|---|---|---|
| `work_id` | integer | item `id` |
| `title` | string, 1..160 | item `title`, truncated at 160 |
| `priority` | integer 1..9 or null | item `priority` |
| `status` | `pending\|active\|done\|blocked` | item `status` |
| `blocked` | boolean | derived from `dep` rows: any blocker whose status is not `done` |
| `updated_at` | string | item `updated_at` |

Item adds: `created_at` (string), `resolution` (string or null),
`blocked_by` (array of `work_id`, the unresolved blockers).

The list returns every item whose status is not `done`, ordered by native
priority (unset last), then creation order — the next-work order. A consumer
wanting only ready work filters on `blocked`.

## Never emitted

`description`, `assignee`, `repo_id`, `sprint_id`, `track_id`,
`aggregate_uuid`, `legacy`, `terminal_decision_id`, `edit_revision`,
`status_revision`, `provenance`, `tier`, `prior_attempts`.

Each is named in `tests/test_work_public_contract.py` as a field that must be
absent from the handler's output and must fail the schema if it appears.
`blocked` is never derived from description text; a description that says
"Blocked-on" with no dep row is not blocked, and the test proves it.

## Adding a field

1. Classify the source column: public or never-emit
   (`PUBLIC_WORK_NEVER_EMIT_FIELDS` in `sprintctl/contracts.py`). The test
   `test_never_emit_list_covers_every_key_the_live_record_carries_beyond_the_public_set`
   fails until every record key is classified.
2. Add it to the schema in `sprintctl/vuoro_adapter.py` and to the explicit
   field list in `WorkApplication._public_list_fields` / `_public_item`. The
   handler never spreads `**item`.
3. Force the gate: inject the field into a response and show the old schema
   rejects it, before widening the schema.
