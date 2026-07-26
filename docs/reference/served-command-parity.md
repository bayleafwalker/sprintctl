# Served command parity matrix

Status: current implementation inventory (2026-07-26).  This is the command
level companion to `docs/plans/vuoro-served-authority-alignment.md`.  A
`served` command means it invokes the Vuoro catalog and never opens a direct
store.  `Unavailable` likewise never opens a store: it exits with the stable
`served-operation-unavailable` error instead of suggesting PostgreSQL support.

| Blind-agent loop command | Served status | Catalog operation / current guidance |
| --- | --- | --- |
| `usage --context` | Served | `work.read.context` returns the complete frozen ContextContract v1 from one server-side repeatable-read aggregate; `--project` remains unavailable. |
| `item list` | Unavailable | Needs filtered `work.read.items`. |
| `item show` | Served | `work.read.item`, includes refs, dependencies, and active claims. |
| `item ref list`, `item dep list` | Unavailable | Inspect the corresponding sections of `item show` for a known item; dedicated routes remain P0. |
| `item ref add/remove`, `item dep add/remove` | Unavailable | Need authenticated shaping-write operations. |
| `next-work` | Served, except `--explain` | `work.read.next-work`; `--explain` needs conflict/exclusion data. |
| `claim start/heartbeat/handoff/release` | Served | `work.claim.start` / `work.claim.arbitrate`. |
| `claim list`, `claim list-sprint`, `claim show`, `claim resume` | Unavailable | Item inspection exposes active claims only; resume and proof disclosure require dedicated designs. |
| `item add`, `item note`, `item status`, `event add/list` | Served | Existing catalog routes. |
| `item done-from-claim` | Unavailable | The two served steps, `item status --status done` then `claim release`, remain available; their local atomic convenience command needs a catalog operation. |
| `handoff` | Unavailable | Needs a served tracker-handoff read plus an append-only generated-handoff record. |

`claim recover` remains recovery-only because it reads a local token sidecar;
in served mode it fails closed rather than opening that local store.  It must
never be represented as remote proof discovery.  Project-oriented
commands and `sprint show --detail` are separately unavailable until their
aggregate catalog contracts exist.
