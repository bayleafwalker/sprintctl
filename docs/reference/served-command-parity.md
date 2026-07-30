# Served command parity matrix

Status: current implementation inventory (2026-07-26).  This is the command
level companion to `docs/plans/vuoro-served-authority-alignment.md`.  A
`served` command means it invokes the Vuoro catalog and never opens a direct
store.  `Unavailable` likewise never opens a store: it exits with the stable
`served-operation-unavailable` error instead of suggesting PostgreSQL support.

| Blind-agent loop command | Served status | Catalog operation / current guidance |
| --- | --- | --- |
| `usage --context` | Served | `work.read.context` returns the complete frozen ContextContract v1 from one server-side repeatable-read aggregate. `--project` uses `work.project.context` only with a canonical server binding and authorization for every member. |
| `context-candidates` | Served | `work.read.context-candidates` builds the bounded Tier-1 packet at the repository authority. Only a found, pending explicit target is claim-eligible; this read never acquires a claim. |
| `item list` | Served | `work.read.items` returns filtered repository-scoped rows; `--project` uses `work.project.items`; `--fzf` remains unavailable. |
| `item show` | Served | `work.read.item`, includes refs, dependencies, and active claims. |
| `item ref list`, `item dep list` | Served | `work.read.item` supplies the exact item-scoped reference/dependency views. |
| `item ref add/remove`, `item dep add/remove` | Served | `work.item.ref.*` and `work.item.dep.*` are repository-scoped shaping writes. |
| `next-work` | Served; project `--explain` unavailable | `work.read.next-work` preserves the list contract; `work.read.next-work-explain` returns the complete atomic explanation contract. |
| `claim start/heartbeat/handoff/release` | Served | `work.claim.start` / `work.claim.arbitrate`. |
| `claim list`, `claim list-sprint`, `claim show`, `claim resume` | Served | `work.read.claims` supports item/sprint/identity inspection; `work.read.claim` is deliberately non-secret. |
| `item add`, `item note`, `item status`, `event add/list` | Served | Existing catalog routes. |
| `item done-from-claim` | Served | One durable `item.done-from-claim` authority command through `work.lifecycle.arbitrate`; the claim proof is transient and retries reuse its immutable event id. |
| `handoff` | Served | `work.read.handoff` builds the tracker snapshot; after local artifact output, `work.handoff.record` appends the authenticated tracker record. An unconfirmed record exits nonzero without discarding the artifact. |

`claim recover` is served-catalog-aware.  It reads the served active claim
(via `work.read.claim` or `work.read.claims`) and reads a caller-local sidecar
file.  Before returning the token it validates sidecar object shape, a nonempty
string token, and exact equality of `claim_id`, `work_item_id`, `actor`, and
`claim_type` between the sidecar and the served active claim.  Both `--id` and
`--item-id` reject inactive claims, malformed/missing/empty sidecars, and every
identity mismatch without printing a token.  The sidecar is written by
`claim start` (and other claim-minting commands) to the local filesystem even
in served mode; it never opens a local work store or writes authority/outbox
state. `sprint list --project` uses
`work.project.sprints` under the same canonical-binding and per-member-
authorization gate. Project aggregates never read a client-side `project.toml`;
without the server binding they fail closed. Each member uses its own
repeatable-read snapshot, preserves canonical order and `origin_repo`, and
reports unavailable members without discarding authorized peers. `sprint show --detail` is served by the server-side
`work.read.sprint-detail` aggregate.

The source catalog is not a deployment assertion: Vuoro composition must construct
`ProjectWorkApplication` from its canonical binding (including ordered `backlog_repos`)
before these operations appear in a released served profile.
